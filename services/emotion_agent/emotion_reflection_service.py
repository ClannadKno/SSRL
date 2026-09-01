# -*- coding: utf-8 -*-
"""Emotion Agent - Core generation service.

Generates an emotion reflection message for a group/session/window
and persists run records.
"""

import json
import logging
import random
import re
import sqlite3
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Optional

from config import EMOTION_INTERVAL_SECONDS
from db import (
    db, execute, now_str, query_one, query_all,
    get_session_agent_config, get_current_running_session_context,
    get_current_learning_task, get_sera_user_id,
    create_agent_research_event, update_agent_research_event,
    attach_message_to_agent_event,
)
from services.llm_gateway import get_gateway
from services.emotion_agent.emotion_feedback_classifier import (
    EmotionFeedbackClassifier,
)
from services.emotion_agent.emotion_feedback_generator import (
    EMOTION_FEEDBACK_TYPES as E2_EMOTION_FEEDBACK_TYPES,
    EmotionFeedbackGenerator,
)
from knowledge_base import STATE_META

logger = logging.getLogger(__name__)

FALLBACK_MESSAGES = {
    "positive_collaboration": (
        "大家的投入和互相补充很清晰，稳稳保持就好 😊",
        "小组的认真交流很有力量，照着自己的节奏就好 🌿",
    ),
    "blocked_frustration": (
        "思路暂时不清晰也很正常，慢慢来就好 🌿",
        "这个过程不容易，大家已经在认真面对了 💛",
    ),
    "conflict_tension": (
        "不同看法出现很正常，大家的感受都值得被看见 🌿",
        "观点有碰撞并不糟，愿意表达本身就很珍贵 🤝",
    ),
    "task_detached": (
        "偶尔分散一下也没关系，重新聚焦时放轻松些 🌱",
        "节奏稍有偏移也没关系，大家从容一点就好 🌿",
    ),
    "negative_silence": (
        "安静片刻也没关系，小组按自己的节奏来就好 🌙",
        "短暂留白也很自然，大家不必有压力 🌿",
    ),
    "unknown": (
        "大家按自己的节奏交流就好，我们一直陪着你们 🌿",
        "每段讨论都有自己的节奏，大家从容一点就好 🌸",
    ),
}

EMOTION_FEEDBACK_SCHEMA_VERSION = "emotion_feedback_v1"

# Re-export the Stage E2 library for compatibility with existing imports.
EMOTION_FEEDBACK_TYPES = E2_EMOTION_FEEDBACK_TYPES
EMOTION_FEEDBACK_TYPES = E2_EMOTION_FEEDBACK_TYPES

FEEDBACK_TYPE_MARKERS = {
    "GROUP_EXCELLENT": ("积极", "投入", "主动", "活跃", "认真", "很棒", "不错"),
    "GROUP_IMPROVING": ("进步", "提升", "增加", "更积极", "更主动", "越来越"),
    "GROUP_DECLINING": ("下降", "减少", "少了一些", "回落", "放缓"),
    "GROUP_LOW_PARTICIPATION": ("较低", "比较少", "还不多", "安静", "更多一些", "发挥空间"),
    "GROUP_SUSTAINED_EXCELLENT": ("持续", "连续", "一直", "稳定", "保持"),
}

GROUP_FEEDBACK_PRESSURE_MARKERS = (
    "老师", "榜样", "不要松懈", "别停下脚步", "真实实力",
    "别害羞", "小组也需要你的声音", "你们一定行",
)

FORBIDDEN_WORDS = [
    "建议", "可以先", "大家每人",
    "轮流", "分工", "下一步",
    "讨论一下", "说一说", "写下来",
    "总结", "记录", "回到任务",
    "完成任务", "你们应该", "你应该",
    "不妨", "试着", "尝试列出",
]
TASK_SUGGESTION_PATTERNS = (
    re.compile(
        r"(?:大家|小组|各位|你们)?"
        r"(?:可以|应该|需要|不妨|试着|尝试|请)"
        r"(?:先|再|一起)?[^，。！？]{0,12}"
        r"(?:讨论|分析|列出|写|记录|总结|分工|轮流|回答|决定|完成)"
    ),
    re.compile(
        r"(?:^|[，。！？])(?:先|再|一起)"
        r"(?:讨论|分析|列出|写|记录|总结|分工|轮流|回答|决定|完成)"
    ),
)

MAX_MESSAGES_FOR_CONTEXT = 5
MAX_STUDENT_MESSAGES_IN_PROMPT = 6
MAX_MESSAGE_CHARS = 120
STATE_FRESHNESS_SECONDS = 180

STATE_CODE_ALIASES = {
    "positive_collaboration": "positive_collaboration",
    "negative_silence": "negative_silence",
    "conflict_tension": "conflict_tension",
    "blocked_frustration": "blocked_frustration",
    "frustration_stuck": "blocked_frustration",
    "task_detached": "task_detached",
    "off_task": "task_detached",
}
RISK_STATE_CODES = frozenset({
    "negative_silence",
    "conflict_tension",
    "blocked_frustration",
    "task_detached",
})


PERSONAL_TARGETING = [
    "学生A", "学生B", "某位同学",
    "有的同学",
]
PERSONAL_TARGETING_PATTERNS = (
    re.compile(r"成员\s*[A-Za-z0-9一二三四五六七八九十]+"),
    re.compile(r"第\s*[一二三四五六七八九十0-9]+\s*位同学"),
    re.compile(r"[\u4e00-\u9fffA-Za-z]{1,8}同学"),
)

STATE_CONTRADICTION_MARKERS = {
    "positive_collaboration": (
        "卡住", "卡顿", "紧张", "焦虑", "压力", "停顿",
        "不容易", "分歧", "冲突", "沉默", "着急",
    ),
    "blocked_frustration": (
        "配合得很好", "讨论很顺利", "进展很快", "成果很棒",
        "节奏特别好", "毫无困难",
    ),
    "conflict_tension": (
        "配合得很好", "讨论很顺利", "毫无分歧", "大家完全一致",
        "成果很棒",
    ),
    "task_detached": (
        "卡住", "焦虑", "冲突", "分歧", "配合得很好",
        "讨论很顺利",
    ),
    "negative_silence": (
        "为什么都不说话", "不要沉默", "赶紧说", "必须开口",
    ),
    "unknown": (
        "配合得很好", "讨论很顺利", "大家很紧张", "都卡住了",
        "出现冲突", "发生分歧", "一直沉默",
    ),
}

EMOTION_SEMANTIC_MARKERS = {
    "positive": ("投入", "配合", "积极", "顺利", "进展", "很棒", "互相补充", "认真交流"),
    "frustration": ("卡住", "不容易", "困难", "费劲", "着急", "不清晰"),
    "conflict": ("分歧", "不同看法", "观点", "碰撞", "不一致"),
    "detached": ("聚焦", "跑题", "分散", "偏移"),
    "silence": ("沉默", "安静", "停顿", "留白"),
}

class EmotionReflectionService:
    """Emotion Agent core service."""

    @staticmethod
    def execute_once(
        group_id: int,
        session_id: Optional[int] = None,
        discussion_id: Optional[int] = None,
        task_id: Optional[int] = None,
        scheduled_at: Optional[str] = None,
        tick_index: Optional[int] = None,
        slot_id: Optional[int] = None,
        window_start: Optional[str] = None,
        window_end: Optional[str] = None,
        frozen_context: Optional[dict] = None,
    ) -> dict:
        """Execute one emotion reflection generation."""
        actual_started_at = now_str()
        if window_end is None:
            window_end = actual_started_at
        if window_start is None:
            fallback_interval = max(1, int(EMOTION_INTERVAL_SECONDS or 300))
            window_start = (
                datetime.now() - timedelta(seconds=fallback_interval)
            ).strftime("%Y-%m-%d %H:%M:%S")

        config = get_session_agent_config(session_id=session_id, group_id=group_id)
        enabled = bool(config.get("emotion_agent_enabled", False))

        if not session_id or not task_id:
            ctx = get_current_running_session_context()
            if ctx:
                session_id = session_id or ctx.get("session_id")
                task_id = task_id or ctx.get("task_id")

        from services.intervention_pipeline_v2.agent_research_helper import (
            build_teacher_config_snapshot,
        )
        teacher_config_json = build_teacher_config_snapshot(group_id)

        trigger_reason = json.dumps({
            "tick_index": tick_index,
            "slot_index": tick_index,
            "slot_id": slot_id,
            "window_start": window_start,
            "window_end": window_end,
            "group_id": group_id,
            "session_id": session_id,
            "discussion_id": discussion_id,
            "prompt_version": (
                frozen_context.get("prompt_version") if frozen_context else None
            ),
        }, ensure_ascii=False)

        if not enabled:
            event_id = create_agent_research_event(
                group_id=group_id, agent_type="emotion",
                event_type="emotion_reflection_skipped",
                enabled_by_config=0, trigger_type="emotion_time_slot",
                trigger_reason_json=trigger_reason,
                session_id=session_id, task_id=task_id,
                skip_reason="emotion_agent_disabled",
                context_snapshot_json=json.dumps(
                    {"enabled_by_config": 0}, ensure_ascii=False
                ),
            )
            return {"status": "skipped", "reason": "emotion_agent_disabled",
                    "event_id": event_id}

        context = EmotionReflectionService.build_context(
            group_id=group_id, session_id=session_id, task_id=task_id,
            discussion_id=discussion_id, slot_id=slot_id, slot_index=tick_index,
            window_start=window_start, window_end=window_end,
            max_messages=MAX_MESSAGES_FOR_CONTEXT,
            max_chars=MAX_MESSAGE_CHARS,
            frozen_context=frozen_context,
        )

        gateway = get_gateway()
        classification = EmotionReflectionService.classify_feedback(
            context,
            gateway=gateway,
        )
        if not classification.get("success"):
            return {
                "status": "failed",
                "reason": "emotion_stage_e1_failed",
                "error": classification.get("failure_reason")
                or "emotion feedback classification failed",
                "retryable": False,
                "classification_attempt_count": classification.get("attempt_count", 0),
            }
        reference_templates = EmotionReflectionService._select_feedback_references(
            classification["feedback_type_code"],
            previous_message=context.get("previous_emotion_message"),
        )
        feedback_plan = {
            "schema_version": EMOTION_FEEDBACK_SCHEMA_VERSION,
            "feedback_type_code": classification["feedback_type_code"],
            "feedback_type_label": classification["feedback_type_label"],
            "confidence": classification.get("confidence"),
            "reason": classification.get("reason"),
            "decision_source": classification.get("decision_source"),
            "metrics": classification.get("metrics") or {},
            "reference_templates": reference_templates,
        }
        context["participation_feedback"] = feedback_plan

        e2_input = EmotionFeedbackGenerator.build_input(
            classification,
            student_messages=context.get("current_student_messages") or [],
            reference_templates=reference_templates,
            recent_emotion_messages=context.get("recent_emotion_feedbacks") or [],
        )
        generation = EmotionFeedbackGenerator.generate(
            e2_input,
            gateway=gateway,
            slot_id=slot_id,
            reference_template_ids=[
                item["template_id"] for item in reference_templates
            ],
            member_labels=EmotionReflectionService._get_member_identity_tokens(
                group_id
            ),
        )
        if not generation.get("success"):
            return {
                "status": "failed",
                "reason": "emotion_stage_e2_failed",
                "error": generation.get("failure_reason")
                or "emotion feedback generation failed",
                "retryable": False,
                "generation_id": generation.get("generation_id"),
            }
        prompt_data = {
            "schema_version": EMOTION_FEEDBACK_SCHEMA_VERSION,
            "classification": classification.get("prompt_data"),
            "generation": generation.get("prompt_data"),
            "feedback_type_code": classification["feedback_type_code"],
            "feedback_type_label": classification["feedback_type_label"],
            "reference_template_ids": [
                item["template_id"] for item in reference_templates
            ],
        }
        llm_result = generation["llm_result"]
        feedback_type_code = classification["feedback_type_code"]
        feedback_type_label = classification["feedback_type_label"]
        message_situation = generation.get("message_situation")
        situation_summary = generation.get("situation_summary")
        fallback_used = 1 if generation.get("fallback_used") else 0
        message = generation["final_text"]
        event_type = (
            "emotion_reflection_fallback"
            if fallback_used
            else "emotion_reflection_published"
        )
        fallback_reason = str(
            generation.get("fallback_reason") or "invalid_output"
        )
        skip_reason = (
            fallback_reason
            if fallback_used and fallback_reason == "llm_declined_output_forced"
            else (
                "stage_e2_fallback: " + fallback_reason
                if fallback_used
                else None
            )
        )
        raw_content = generation.get("raw_content")
        if raw_content is not None and not isinstance(raw_content, str):
            raw_content = json.dumps(raw_content, ensure_ascii=False, default=str)
        fallback_state_code = feedback_type_code if fallback_used else None
        fallback_message = message if fallback_used else None
        final_disposition = (
            "fallback_published" if fallback_used else "model_published"
        )
        model_validation = generation.get("model_validation") or {
            "valid": False,
            "reason": "missing_model_validation",
            "failure_codes": ["missing_model_validation"],
            "checks": {},
        }
        fallback_validation = generation.get("fallback_validation")

        validation_result = EmotionReflectionService._build_validation_audit(
            model_validation,
            fallback_validation=fallback_validation,
            final_passed=bool(message),
        )
        generation_failures = []
        for validation in generation.get("validations") or []:
            generation_failures.extend(validation.get("failure_codes") or [])
        validation_result["validation_failure_codes"] = list(
            dict.fromkeys(
                generation_failures
                + list(validation_result.get("validation_failure_codes") or [])
            )
        )
        validation_result["generation_validation_status"] = generation.get(
            "validation_status"
        )
        emotion_audit = EmotionReflectionService._build_emotion_audit(
            context=context,
            slot_id=slot_id,
            model_raw_message=raw_content,
            validation_result=validation_result,
            fallback_state_code=fallback_state_code,
            fallback_message=fallback_message,
            final_visible_message=message,
            final_disposition=final_disposition,
            classification=classification,
            reference_templates=reference_templates,
        )
        emotion_audit["message_situation"] = message_situation
        emotion_audit["situation_summary"] = situation_summary

        run_id = EmotionReflectionService._create_intervention_run(
            group_id=group_id,
            status="PENDING",
            generated_message=message, fallback_used=fallback_used,
            context=context, prompt_data=prompt_data,
            llm_result=llm_result, validation_result=validation_result,
            teacher_config_json=teacher_config_json,
            window_start=window_start, window_end=window_end,
            scheduled_at=scheduled_at, actual_started_at=actual_started_at,
            session_id=session_id, discussion_id=discussion_id,
            session_no=context.get("session_no"),
            task_id=task_id, tick_index=tick_index, slot_id=slot_id,
            skip_reason=skip_reason,
            emotion_audit=emotion_audit,
        )

        message_id = None
        terminal_result_status = None
        terminal_strategy_run_id = None
        terminal_superseded_by_slot_id = None
        if message:
            published = EmotionReflectionService._publish_emotion_message(
                group_id=group_id,
                session_id=session_id,
                discussion_id=discussion_id,
                task_id=task_id,
                session_no=context.get("session_no"),
                message=message,
                intervention_run_id=run_id,
                emotion_slot_id=slot_id,
                expected_student_sequence=(
                    None
                    if context.get("window_frozen")
                    else emotion_audit.get("context_student_sequence_end")
                ),
                fallback_used=bool(fallback_used),
                feedback_metadata={
                    "schema_version": EMOTION_FEEDBACK_SCHEMA_VERSION,
                    "feedback_type_code": feedback_type_code,
                    "feedback_type_label": feedback_type_label,
                    "message_situation": message_situation,
                    "situation_summary": situation_summary,
                    "reference_template_ids": [
                        item["template_id"] for item in reference_templates
                    ],
                },
            )
            if not published.get("ok"):
                slot_status = published.get("slot_status")
                EmotionFeedbackGenerator.mark_not_published(
                    generation.get("generation_id"),
                    status=str(slot_status or "FAILED").upper(),
                    reason=published.get("reason") or "publish_failed",
                )
                if slot_status in {"deferred", "suppressed", "expired", "superseded"}:
                    terminal_result_status = slot_status
                    terminal_strategy_run_id = published.get("strategy_run_id")
                    terminal_superseded_by_slot_id = published.get(
                        "superseded_by_slot_id"
                    )
                    skip_reason = published.get("reason") or (
                        "suppressed_after_generation"
                        if slot_status == "suppressed"
                        else slot_status + "_after_generation"
                    )
                    final_disposition = published.get("final_disposition") or skip_reason
                    emotion_audit["final_disposition"] = final_disposition
                    emotion_audit["coordination_gate"] = {
                        "slot_status": slot_status,
                        "reason": skip_reason,
                        "strategy_run_id": terminal_strategy_run_id,
                        "superseded_by_slot_id": terminal_superseded_by_slot_id,
                    }
                    event_type = "emotion_reflection_" + slot_status
                    run_status = published.get("run_status") or {
                        "deferred": "STALE",
                        "suppressed": "SUPPRESSED",
                        "expired": "EXPIRED",
                        "superseded": "SUPERSEDED",
                    }[slot_status]
                    execute(
                        """
                        UPDATE intervention_runs
                           SET status=?, decision='SKIPPED', skip_reason=?,
                               failure_reason=?, completed_at=?,
                               final_disposition=?, emotion_audit_json=?
                         WHERE id=? AND message_id IS NULL
                        """,
                        (
                            run_status,
                            skip_reason,
                            skip_reason,
                            now_str(),
                            final_disposition,
                            json.dumps(emotion_audit, ensure_ascii=False, default=str),
                            run_id,
                        ),
                    )
                    message = None
                else:
                    emotion_audit["final_disposition"] = "publish_failed"
                    execute(
                        """
                        UPDATE intervention_runs
                        SET status='FAILED', failure_reason=?, completed_at=?,
                            final_disposition='publish_failed', emotion_audit_json=?
                        WHERE id=? AND message_id IS NULL
                        """,
                        (
                            published.get("reason") or "publish_failed",
                            now_str(),
                            json.dumps(emotion_audit, ensure_ascii=False, default=str),
                            run_id,
                        ),
                    )
                    return {
                        "status": "failed",
                        "reason": published.get("reason") or "publish_failed",
                        "error": published.get("error"),
                        "run_id": run_id,
                        "generation_id": generation.get("generation_id"),
                    }
            message_id = published.get("message_id")
            if published.get("ok") and message_id:
                EmotionFeedbackGenerator.mark_published(
                    generation.get("generation_id"), int(message_id)
                )
        else:
            execute(
                """
                UPDATE intervention_runs
                   SET status='SKIPPED', decision='SKIPPED',
                       completed_at=?, final_disposition=?
                 WHERE id=? AND message_id IS NULL
                """,
                (now_str(), final_disposition, run_id),
            )

        validation_json = EmotionReflectionService._build_validation_json(
            validation_result
        )

        event_id = create_agent_research_event(
            group_id=group_id, agent_type="emotion",
            event_type=event_type,
            enabled_by_config=1 if enabled else 0,
            trigger_type="emotion_time_slot",
            trigger_reason_json=trigger_reason,
            context_snapshot_json=json.dumps(
                context, ensure_ascii=False, default=str
            ),
            llm_prompt_json=json.dumps(prompt_data, ensure_ascii=False),
            llm_response_json=json.dumps({
                "success": llm_result.success, "output": llm_result.output,
                "raw_text": llm_result.raw_text,
                "failure_type": llm_result.failure_type,
                "latency_ms": llm_result.latency_ms,
            }, ensure_ascii=False),
            validation_json=validation_json,
            intervention_run_id=run_id, message_id=message_id,
            session_id=session_id, task_id=task_id,
            skip_reason=skip_reason, scheduled_at=scheduled_at,
            metadata_json=json.dumps(
                {"emotion_audit": emotion_audit},
                ensure_ascii=False,
                default=str,
            ),
        )

        if message_id and event_id:
            try:
                attach_message_to_agent_event(event_id, message_id, "emotion")
            except Exception:
                pass

        return {
            "status": terminal_result_status or (
                "skipped" if not message
                else ("published" if not fallback_used else "fallback")
            ),
            "reason": skip_reason if not message else None,
            "message": message, "message_id": message_id,
            "run_id": run_id, "event_id": event_id,
            "strategy_run_id": terminal_strategy_run_id,
            "superseded_by_slot_id": terminal_superseded_by_slot_id,
            "fallback_used": fallback_used, "skip_reason": skip_reason,
            "generation_id": generation.get("generation_id"),
            "generation_validation_status": generation.get("validation_status"),
            "feedback_type_code": feedback_type_code,
            "feedback_type_label": feedback_type_label,
            "message_situation": message_situation,
            "situation_summary": situation_summary,
        }

    @staticmethod
    def build_context(group_id, session_id=None, discussion_id=None,
                       task_id=None, slot_id=None, slot_index=None,
                       window_start=None, window_end=None,
                       max_messages=MAX_MESSAGES_FOR_CONTEXT,
                       max_chars=MAX_MESSAGE_CHARS,
                       frozen_context=None):
        """Build context for LLM."""
        ctx = get_current_running_session_context()
        resolved_session_id = session_id or (ctx.get("session_id") if ctx else None)
        session_row = query_one(
            "SELECT session_no, task_id FROM experiment_sessions WHERE id=?",
            (resolved_session_id,),
        ) if resolved_session_id else None
        resolved_task_id = (
            task_id
            or (session_row["task_id"] if session_row else None)
            or (ctx.get("task_id") if ctx else None)
        )
        task_row = query_one(
            "SELECT * FROM learning_tasks WHERE id=?", (resolved_task_id,)
        ) if resolved_task_id else None
        task = dict(task_row) if task_row else get_current_learning_task()

        state_context = EmotionReflectionService._get_latest_dominant_state(
            group_id,
            session_id=resolved_session_id,
            discussion_id=discussion_id,
            current_time=window_end,
        )
        dominant_state = state_context["dominant_state"]
        if frozen_context:
            recent = [
                dict(item)
                for item in (frozen_context.get("current_messages") or [])
            ]
            previous_student_messages = [
                dict(item)
                for item in (frozen_context.get("previous_messages") or [])
            ]
        else:
            recent = EmotionReflectionService._get_recent_messages(
                group_id, window_start, window_end,
                session_id=resolved_session_id,
                discussion_id=discussion_id,
                max_messages=max_messages, max_chars=max_chars,
                evidence_sequences=dominant_state.get("evidence_sequences"),
            )
            previous_student_messages = []
        checkin = EmotionReflectionService._get_checkin_summary(
            group_id, window_start, window_end, session_id=resolved_session_id
        )
        msg_summary = EmotionReflectionService._build_message_summary(recent)
        interaction = EmotionReflectionService._build_interaction_summary(recent)
        state_summary = EmotionReflectionService._build_state_summary(dominant_state)
        previous_emotion = EmotionReflectionService._get_previous_emotion_message(
            group_id,
            session_id=resolved_session_id,
        )
        recent_emotion_feedbacks = (
            EmotionReflectionService._get_recent_emotion_feedbacks(
                group_id,
                session_id=resolved_session_id,
                discussion_id=discussion_id,
            )
        )
        if frozen_context:
            current_metrics = dict(frozen_context.get("current_metrics") or {})
            previous_metrics = dict(frozen_context.get("previous_metrics") or {})
            expected = 0
            if discussion_id is not None:
                expected_row = query_one(
                    """
                    SELECT expected_student_count
                    FROM group_session_discussions
                    WHERE id=? AND group_id=? AND session_id=?
                    """,
                    (discussion_id, group_id, resolved_session_id),
                )
                if expected_row:
                    expected = int(expected_row["expected_student_count"] or 0)
            current_count = int(current_metrics.get("message_count") or 0)
            previous_count = int(previous_metrics.get("message_count") or 0)
            participation_metrics = {
                "previous_window_start": frozen_context.get("previous_window_start"),
                "previous_window_end": frozen_context.get("previous_window_end"),
                "current_window_start": frozen_context.get("current_window_start"),
                "current_window_end": frozen_context.get("current_window_end"),
                "window_seconds": int(
                    (
                        EmotionReflectionService._parse_datetime(
                            frozen_context.get("current_window_end")
                        )
                        - EmotionReflectionService._parse_datetime(
                            frozen_context.get("current_window_start")
                        )
                    ).total_seconds()
                ),
                "current_message_count": current_count,
                "previous_message_count": previous_count,
                "message_count_delta": current_count - previous_count,
                "message_count_ratio": (
                    round(current_count / previous_count, 3)
                    if previous_count > 0
                    else None
                ),
                "current_active_member_count": int(
                    current_metrics.get("active_member_count") or 0
                ),
                "previous_active_member_count": int(
                    previous_metrics.get("active_member_count") or 0
                ),
                "expected_member_count": expected,
                "current_metrics": current_metrics,
                "previous_metrics": previous_metrics,
                "evidence_complete": True,
            }
        else:
            participation_metrics = EmotionReflectionService._get_participation_metrics(
                group_id=group_id,
                session_id=resolved_session_id,
                discussion_id=discussion_id,
                window_start=window_start,
                window_end=window_end,
            )

        return {
            "session_id": resolved_session_id,
            "discussion_id": discussion_id,
            "emotion_slot_id": slot_id,
            "emotion_slot_index": slot_index,
            "session_no": (
                session_row["session_no"]
                if session_row
                else (ctx.get("session_no") if ctx else None)
            ),
            "task_id": resolved_task_id or (task.get("id") if task else None),
            "task_title": (task.get("title") or task.get("question") or "") if task else "",
            "task_question": (task.get("question") or "") if task else "",
            "recent_messages": recent,
            "recent_student_messages": recent,
            "current_student_messages": recent,
            "previous_student_messages": previous_student_messages,
            "message_count": len(recent),
            "latest_group_state": dominant_state,
            "latest_monitor_state": state_context["latest_monitor_state"],
            "latest_batch_state": state_context["latest_batch_state"],
            "dominant_state": dominant_state,
            "state_has_recovered": state_context["state_has_recovered"],
            "recovery_evidence_sequences": state_context[
                "recovery_evidence_sequences"
            ],
            "checkin_summary": checkin,
            "message_summary": msg_summary,
            "interaction_summary": interaction,
            "state_summary": state_summary,
            "previous_emotion_message": previous_emotion,
            "previous_emotion_run_id": (
                previous_emotion.get("run_id") if previous_emotion else None
            ),
            "previous_emotion_state": (
                previous_emotion.get("state_code") if previous_emotion else None
            ),
            "previous_emotion_published_at": (
                previous_emotion.get("published_at") if previous_emotion else None
            ),
            "recent_emotion_feedbacks": recent_emotion_feedbacks,
            "participation_metrics": participation_metrics,
            "window_start": window_start,
            "window_end": window_end,
            "window_frozen": bool(frozen_context),
            "window_frozen_at": (
                frozen_context.get("window_frozen_at") if frozen_context else None
            ),
            "emotion_prompt_version": (
                frozen_context.get("prompt_version") if frozen_context else None
            ),
            "frozen_input_message_ids": (
                list(frozen_context.get("input_message_ids") or [])
                if frozen_context
                else []
            ),
            "frozen_input_sequences": (
                [
                    int(item["sequence"])
                    for item in previous_student_messages + recent
                    if item.get("sequence") is not None
                ]
                if frozen_context
                else []
            ),
        }

    @staticmethod
    def _get_participation_metrics(
        *,
        group_id,
        session_id=None,
        discussion_id=None,
        window_start=None,
        window_end=None,
    ):
        """Compare the current fixed slot with the preceding equal slot."""
        start_dt = EmotionReflectionService._parse_datetime(window_start)
        end_dt = EmotionReflectionService._parse_datetime(window_end)
        if not start_dt or not end_dt or end_dt <= start_dt:
            return {
                "current_message_count": 0,
                "previous_message_count": 0,
                "current_active_member_count": 0,
                "previous_active_member_count": 0,
                "expected_member_count": 0,
                "window_seconds": None,
                "evidence_complete": False,
            }

        window_seconds = max(1, int((end_dt - start_dt).total_seconds()))
        previous_start_dt = start_dt - timedelta(seconds=window_seconds)
        previous_start = previous_start_dt.strftime("%Y-%m-%d %H:%M:%S")
        current_start = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        current_end = end_dt.strftime("%Y-%m-%d %H:%M:%S")

        def count_window(left, right):
            clauses = [
                "group_id=?",
                "created_at>=?",
                "created_at<?",
                "role='student'",
            ]
            params = [group_id, left, right]
            if session_id is not None:
                clauses.append("session_id=?")
                params.append(session_id)
            if discussion_id is not None:
                clauses.append("discussion_id=?")
                params.append(discussion_id)
            row = query_one(
                """
                SELECT COUNT(*) AS message_count,
                       COUNT(DISTINCT user_id) AS active_member_count
                FROM messages
                """
                + " WHERE "
                + " AND ".join(clauses),
                tuple(params),
            )
            return {
                "message_count": int(row["message_count"] or 0) if row else 0,
                "active_member_count": (
                    int(row["active_member_count"] or 0) if row else 0
                ),
            }

        current = count_window(current_start, current_end)
        previous = count_window(previous_start, current_start)

        expected = 0
        if discussion_id is not None:
            scope = query_one(
                """
                SELECT expected_student_count
                FROM group_session_discussions
                WHERE id=? AND group_id=?
                """,
                (discussion_id, group_id),
            )
            if scope:
                expected = int(scope["expected_student_count"] or 0)
        if expected < 1:
            member_row = query_one(
                """
                SELECT COUNT(*) AS member_count
                FROM experiment_participants
                WHERE group_id=? AND is_active=1
                """,
                (group_id,),
            )
            expected = int(member_row["member_count"] or 0) if member_row else 0

        current_count = current["message_count"]
        previous_count = previous["message_count"]
        delta = current_count - previous_count
        ratio = (
            round(current_count / previous_count, 3)
            if previous_count > 0
            else None
        )
        return {
            "current_window_start": current_start,
            "current_window_end": current_end,
            "previous_window_start": previous_start,
            "previous_window_end": current_start,
            "window_seconds": window_seconds,
            "current_message_count": current_count,
            "previous_message_count": previous_count,
            "message_count_delta": delta,
            "message_count_ratio": ratio,
            "current_active_member_count": current["active_member_count"],
            "previous_active_member_count": previous["active_member_count"],
            "expected_member_count": expected,
            "evidence_complete": True,
        }

    @staticmethod
    def build_classification_prompt(context):
        """Build the bounded, state/strategy-isolated Stage E1 prompt."""
        return EmotionFeedbackClassifier.build_prompt(context)

    @staticmethod
    def classify_feedback(context, *, gateway=None):
        """Run Emotion Stage E1 without inventing a type on technical failure."""
        return EmotionFeedbackClassifier.classify(context, gateway=gateway)

    @staticmethod
    def _select_feedback_references(
        feedback_type_code,
        *,
        previous_message=None,
        limit=2,
    ):
        config = EMOTION_FEEDBACK_TYPES.get(feedback_type_code)
        if not config:
            config = EMOTION_FEEDBACK_TYPES["GROUP_EXCELLENT"]
        candidates = [
            {"template_id": template_id, "text": text}
            for template_id, text in config["templates"]
        ]
        previous_text = (
            previous_message.get("content")
            if isinstance(previous_message, dict)
            else previous_message
        )
        if previous_text:
            distinct = [
                item
                for item in candidates
                if not EmotionReflectionService._messages_are_too_similar(
                    item["text"], previous_text
                )
            ]
            if distinct:
                candidates = distinct
        size = max(1, min(int(limit or 1), len(candidates)))
        return random.sample(candidates, size)

    @staticmethod
    # Compatibility wrapper for callers that still build prompts through this
    # service.  It projects the broad context onto E2's six allowed fields.
    def build_prompt(context):
        """Build the stage-two group feedback generation prompt."""
        feedback_plan = context.get("participation_feedback")
        if not isinstance(feedback_plan, dict):
            raise ValueError("emotion Stage E2 requires a valid Stage E1 result")
        feedback_type_code = feedback_plan["feedback_type_code"]
        references = feedback_plan.get("reference_templates") or (
            EmotionReflectionService._select_feedback_references(
                feedback_type_code,
                previous_message=context.get("previous_emotion_message"),
            )
        )
        classification = {
            "feedback_state": feedback_type_code,
            "comparison_summary": feedback_plan.get("comparison_summary")
            or feedback_plan.get("reason")
            or "",
            "current_window_summary": feedback_plan.get(
                "current_window_summary"
            )
            or "",
        }
        e2_input = EmotionFeedbackGenerator.build_input(
            classification,
            student_messages=context.get("current_student_messages") or [],
            reference_templates=references,
            recent_emotion_messages=context.get("recent_emotion_feedbacks") or [],
        )
        return EmotionFeedbackGenerator.build_prompt(e2_input)


    @staticmethod
    def validate_message(
        message,
        dominant_state=None,
        previous_message=None,
        member_labels=None,
        feedback_type_code=None,
        feedback_type_label=None,
    ):
        """Validate safety, state consistency and repetition for one message."""
        checks = {
            "non_empty": False,
            "single_sentence": False,
            "length_ok": None,
            "length_checked": False,
            "has_emoji": False,
            "no_suggestion": True,
            "no_personal": True,
            "state_consistent": True,
            "not_duplicate": True,
            "emoji_count_ok": True,
            "group_addressed": True,
            "no_pressure_language": True,
            "feedback_type_consistent": True,
        }
        failure_codes = []
        if not message or not str(message).strip():
            return {
                "valid": False,
                "reason": "empty",
                "failure_codes": ["empty"],
                "checks": checks,
            }

        text = str(message).strip()
        checks["non_empty"] = True

        has_multiline = "\n" in text or "\r" in text
        has_numbering = any(
            text.startswith(str(i) + ".") or text.startswith(str(i) + "\uff09")
            for i in range(1, 10)
        )
        checks["single_sentence"] = not (has_multiline or has_numbering)
        if not checks["single_sentence"]:
            failure_codes.append("multiple_lines_or_numbering")

        emoji_pat = re.compile(
            "[\U0001F300-\U0001F9FF\U0001FA00-\U0001FA6F"
            "\U0001FA70-\U0001FAFF\u2600-\u27BF"
            "\u2B50\u2702-\u27B0"
            "\u00A9\u00AE\u203C\u2049\u2122\u2139"
            "\u2194-\u2199\u21A9\u21AA\u231A\u231B"
            "\u2328\u23CF\u23E9-\u23F3\u23F8-\u23FA"
            "\u24C2\u25AA\u25AB\u25B6\u25C0\u25FB-\u25FE"
            "\u2600-\u27BF\u2934\u2935\u2B05\u2B06\u2B07"
            "\u2B1B\u2B1C\u2B50\u2B55\u3030\u303D\u3297\u3299"
            "\U0001F600-\U0001F64F\U0001F680-\U0001F6FF"
            "\U0001F1E0-\U0001F1FF]+"
        )
        emoji_groups = emoji_pat.findall(text)
        checks["has_emoji"] = bool(emoji_groups)
        checks["emoji_count_ok"] = len(emoji_groups) <= 1
        if feedback_type_code and not checks["emoji_count_ok"]:
            failure_codes.append("too_many_emoji")
        elif not feedback_type_code and not checks["has_emoji"]:
            failure_codes.append("no_emoji")

        suggestion_found = (
            any(word in text for word in FORBIDDEN_WORDS)
            or any(pattern.search(text) for pattern in TASK_SUGGESTION_PATTERNS)
        )
        checks["no_suggestion"] = not suggestion_found
        if suggestion_found:
            failure_codes.append("contains_suggestion_words")

        personal_reason = None
        if re.search(r"你(?!们)", text) or "您" in text:
            personal_reason = "contains_personal_pronoun_you"
        elif any(word in text for word in PERSONAL_TARGETING):
            personal_reason = "contains_personal_targeting"
        elif any(pattern.search(text) for pattern in PERSONAL_TARGETING_PATTERNS):
            personal_reason = "contains_personal_targeting"
        elif member_labels and any(
            str(label).strip() and str(label).strip() in text
            for label in member_labels
        ):
            personal_reason = "contains_personal_targeting"
        checks["no_personal"] = personal_reason is None
        if personal_reason:
            failure_codes.append(personal_reason)

        if feedback_type_code:
            group_terms = ("大家", "小组", "各位", "你们", "共同", "团队")
            checks["group_addressed"] = any(term in text for term in group_terms)
            if not checks["group_addressed"]:
                failure_codes.append("missing_group_address")

            pressure_marker = next(
                (
                    marker
                    for marker in GROUP_FEEDBACK_PRESSURE_MARKERS
                    if marker in text
                ),
                None,
            )
            checks["no_pressure_language"] = pressure_marker is None
            if pressure_marker:
                failure_codes.append(
                    "contains_pressure_language:" + pressure_marker
                )

            type_config = EMOTION_FEEDBACK_TYPES.get(feedback_type_code)
            checks["feedback_type_consistent"] = bool(
                type_config
                and (
                    not feedback_type_label
                    or type_config["label"] == feedback_type_label
                )
                and any(
                    marker in text
                    for marker in FEEDBACK_TYPE_MARKERS.get(
                        feedback_type_code, ()
                    )
                )
            )
            if not checks["feedback_type_consistent"]:
                failure_codes.append(
                    "feedback_type_semantic_conflict:"
                    + str(feedback_type_code)
                )

        if dominant_state is not None:
            state_code = EmotionReflectionService._canonical_state_code(
                dominant_state
            )
            if feedback_type_code:
                contradictions = {
                    "conflict_tension": (
                        "氛围很好", "合作顺利", "毫无分歧", "大家完全一致",
                    ),
                    "blocked_frustration": (
                        "毫无困难", "任务很轻松", "成果已经很好",
                    ),
                    "task_detached": (
                        "始终专注", "一直聚焦",
                    ),
                }.get(state_code, ())
            else:
                contradictions = STATE_CONTRADICTION_MARKERS.get(
                    state_code, ()
                )
            checks["state_consistent"] = not any(
                marker in text for marker in contradictions
            )
            if not checks["state_consistent"]:
                failure_codes.append(
                    "state_semantic_conflict:" + state_code
                )

        previous_text = (
            previous_message.get("content")
            if isinstance(previous_message, dict)
            else previous_message
        )
        if previous_text:
            checks["not_duplicate"] = not (
                EmotionReflectionService._messages_are_too_similar(
                    text, str(previous_text)
                )
            )
            if not checks["not_duplicate"]:
                failure_codes.append("duplicate_previous_emotion_message")

        return {
            "valid": not failure_codes,
            "reason": failure_codes[0] if failure_codes else "ok",
            "failure_codes": failure_codes,
            "checks": checks,
        }

    @staticmethod
    def _messages_are_too_similar(left, right):
        """Return True for exact, lexical or same-category near duplicates."""
        def normalize(value):
            text = re.sub(
                r"[\U0001F300-\U0001FAFF\u2600-\u27BF]",
                "",
                str(value or ""),
            )
            return re.sub(r"[\W_]+", "", text, flags=re.UNICODE).lower()

        left_normalized = normalize(left)
        right_normalized = normalize(right)
        if not left_normalized or not right_normalized:
            return False
        if left_normalized == right_normalized:
            return True
        if (
            min(len(left_normalized), len(right_normalized)) >= 8
            and (
                left_normalized in right_normalized
                or right_normalized in left_normalized
            )
        ):
            return True

        ratio = SequenceMatcher(
            None, left_normalized, right_normalized
        ).ratio()
        if ratio >= 0.78:
            return True

        def categories(value):
            return {
                category
                for category, markers in EMOTION_SEMANTIC_MARKERS.items()
                if any(marker in value for marker in markers)
            }

        shared_categories = categories(left_normalized) & categories(
            right_normalized
        )
        return bool(shared_categories) and ratio >= 0.52

    @staticmethod
    def _get_previous_emotion_message(group_id, session_id=None):
        row = query_one(
            """
            SELECT ir.id AS run_id,
                   ir.message_id,
                   m.content,
                   COALESCE(
                       ir.actual_published_at,
                       ir.published_at,
                       m.created_at
                   ) AS published_at,
                   ir.dominant_state,
                   ir.llm_context_json
              FROM intervention_runs AS ir
              JOIN messages AS m ON m.id=ir.message_id
             WHERE ir.group_id=?
               AND (? IS NULL OR ir.session_id=?)
               AND COALESCE(ir.agent_type, '')='emotion'
               AND ir.status IN ('PUBLISHED', 'FALLBACK')
             ORDER BY COALESCE(
                          ir.actual_published_at,
                          ir.published_at,
                          m.created_at
                      ) DESC,
                      ir.id DESC
             LIMIT 1
            """,
            (group_id, session_id, session_id),
        )
        if not row:
            return None
        result = dict(row)
        state_code = result.pop("dominant_state", None)
        context_json = result.pop("llm_context_json", None)
        if not state_code and context_json:
            try:
                context = json.loads(context_json)
                dominant = (
                    context.get("dominant_state")
                    or context.get("latest_group_state")
                    or {}
                )
                state_code = dominant.get("state_code")
            except (TypeError, ValueError, json.JSONDecodeError):
                state_code = None
        result["state_code"] = (
            EmotionReflectionService._canonical_state_code(state_code)
            if state_code
            else None
        )
        return result

    @staticmethod
    def _get_recent_emotion_feedbacks(
        group_id,
        *,
        session_id=None,
        discussion_id=None,
        limit=2,
    ):
        rows = query_all(
            """
            SELECT ir.emotion_feedback_type_code AS feedback_state,
                   COALESCE(ir.final_visible_message, m.content, '') AS text,
                   COALESCE(ir.actual_published_at, ir.published_at, m.created_at)
                       AS published_at
              FROM intervention_runs AS ir
              JOIN messages AS m ON m.id=ir.message_id
             WHERE ir.group_id=?
               AND (? IS NULL OR ir.session_id=?)
               AND (? IS NULL OR ir.discussion_id=?)
               AND COALESCE(ir.agent_type, '')='emotion'
             ORDER BY COALESCE(
                          ir.actual_published_at,
                          ir.published_at,
                          m.created_at
                      ) DESC,
                      ir.id DESC
             LIMIT ?
            """,
            (
                group_id,
                session_id,
                session_id,
                discussion_id,
                discussion_id,
                max(1, min(int(limit or 2), 2)),
            ),
        )
        return [dict(row) for row in rows]

    @staticmethod
    def _get_member_identity_tokens(group_id):
        rows = query_all(
            """
            SELECT u.username, u.real_name, u.participant_code
              FROM experiment_participants AS ep
              JOIN users AS u ON u.id=ep.user_id
             WHERE ep.group_id=? AND ep.is_active=1
            """,
            (group_id,),
        )
        tokens = []
        for row in rows:
            for field in ("username", "real_name", "participant_code"):
                value = str(row[field] or "").strip()
                if value and value not in tokens:
                    tokens.append(value)
        return tokens

    @staticmethod
    def _get_recent_messages(group_id, window_start, window_end,
                             session_id=None, discussion_id=None,
                              max_messages=MAX_MESSAGES_FOR_CONTEXT,
                              max_chars=MAX_MESSAGE_CHARS,
                              evidence_sequences=None):
        if not window_start or not window_end:
            return []
        try:
            max_messages = max(1, min(int(max_messages), MAX_STUDENT_MESSAGES_IN_PROMPT))
        except (TypeError, ValueError):
            max_messages = MAX_MESSAGES_FOR_CONTEXT
        evidence_sequences = EmotionReflectionService._parse_int_list(
            evidence_sequences
        )
        select_columns = """
            SELECT m.id, m.sequence, m.content, m.created_at,
                   COALESCE(
                       (
                           SELECT '成员' || ep.member_no
                           FROM experiment_participants AS ep
                           WHERE ep.group_id=m.group_id AND ep.user_id=m.user_id
                           ORDER BY ep.is_active DESC, ep.id DESC
                           LIMIT 1
                       ),
                       '成员'
                   ) AS member_label
        """
        scope_clauses = [
            "m.group_id=?",
            "m.created_at>=?",
            "m.created_at<?",
            "m.role='student'",
        ]
        scope_params = [group_id, window_start, window_end]
        if session_id is not None:
            scope_clauses.append("m.session_id=?")
            scope_params.append(session_id)
        if discussion_id is not None:
            scope_clauses.append("m.discussion_id=?")
            scope_params.append(discussion_id)
        rows = query_all(
            select_columns
            + " FROM messages m WHERE "
            + " AND ".join(scope_clauses)
            + " ORDER BY m.created_at DESC, m.id DESC LIMIT ?",
            tuple(scope_params + [max_messages]),
        )

        selected = {int(row["id"]): row for row in rows}
        if evidence_sequences:
            placeholders = ",".join(["?"] * len(evidence_sequences))
            evidence_params = list(scope_params)
            evidence_params.extend(evidence_sequences)
            evidence_rows = query_all(
                select_columns
                + " FROM messages m WHERE "
                + " AND ".join(scope_clauses)
                + f" AND m.sequence IN ({placeholders})"
                + " ORDER BY m.created_at DESC, m.id DESC",
                tuple(evidence_params),
            )
            for row in evidence_rows:
                selected[int(row["id"])] = row

        selected_rows = sorted(
            selected.values(),
            key=lambda row: (
                str(row["created_at"] or ""),
                int(row["sequence"] or 0),
                int(row["id"]),
            ),
            reverse=True,
        )
        hard_limit = min(
            MAX_STUDENT_MESSAGES_IN_PROMPT,
            max_messages + (1 if evidence_sequences else 0),
        )
        if len(selected_rows) > hard_limit:
            evidence_set = set(evidence_sequences)
            pinned = [
                row for row in selected_rows
                if row["sequence"] is not None
                and int(row["sequence"]) in evidence_set
            ]
            others = [
                row for row in selected_rows
                if row["sequence"] is None
                or int(row["sequence"]) not in evidence_set
            ]
            selected_rows = (pinned + others)[:hard_limit]
        selected_rows = list(reversed(selected_rows))

        result = []
        for r in selected_rows:
            d = dict(r)
            d.pop("id", None)
            c = d.get("content") or ""
            if len(c) > max_chars:
                d["content"] = c[:max_chars] + "..."
            result.append(d)
        return result

    @staticmethod
    def _canonical_state_code(value):
        return STATE_CODE_ALIASES.get(str(value or "").strip(), "unknown")

    @staticmethod
    def _parse_int_list(value):
        if value is None:
            return []
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                value = [part.strip() for part in value.split(",")]
        if not isinstance(value, (list, tuple, set)):
            value = [value]
        result = []
        for item in value:
            if isinstance(item, bool):
                continue
            try:
                number = int(item)
            except (TypeError, ValueError):
                continue
            if number not in result:
                result.append(number)
        return sorted(result)

    @staticmethod
    def _parse_datetime(value):
        if isinstance(value, datetime):
            return value
        if not value:
            return None
        text = str(value).strip()
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _freshness_seconds(detected_at, current_time=None):
        detected = EmotionReflectionService._parse_datetime(detected_at)
        current = (
            EmotionReflectionService._parse_datetime(current_time)
            or datetime.now()
        )
        if not detected:
            return None
        if detected.tzinfo is not None and current.tzinfo is None:
            current = current.replace(tzinfo=detected.tzinfo)
        elif detected.tzinfo is None and current.tzinfo is not None:
            detected = detected.replace(tzinfo=current.tzinfo)
        return max(0, int((current - detected).total_seconds()))

    @staticmethod
    def _state_from_segment(row, *, state_source, detected_at):
        if not row:
            return None
        data = dict(row)
        evidence = EmotionReflectionService._parse_int_list(
            data.get("evidence_sequences")
        )
        if not evidence:
            evidence_ids = EmotionReflectionService._parse_int_list(
                data.get("evidence_message_ids_json")
            )
            if evidence_ids:
                placeholders = ",".join(["?"] * len(evidence_ids))
                sequence_rows = query_all(
                    f"""
                    SELECT sequence FROM messages
                    WHERE id IN ({placeholders}) AND sequence IS NOT NULL
                    """,
                    tuple(evidence_ids),
                )
                evidence = EmotionReflectionService._parse_int_list(
                    [item["sequence"] for item in sequence_rows]
                )
        if not evidence:
            evidence = EmotionReflectionService._parse_int_list([
                data.get("start_sequence", data.get("start_message_id")),
                data.get("end_sequence", data.get("end_message_id")),
            ])
        state_code = EmotionReflectionService._canonical_state_code(
            data.get("state_code")
        )
        state_label = STATE_META.get(state_code, STATE_META["unknown"])[0]
        return {
            "state_code": state_code,
            "state_label": state_label,
            "source": state_source,
            "confidence": data.get("confidence"),
            "detected_at": detected_at,
            "evidence_sequences": evidence,
            "evidence_end_sequence": max(evidence) if evidence else None,
            "segment_id": data.get("id"),
        }

    @staticmethod
    def _get_latest_monitor_state(group_id, session_id=None):
        params = [group_id]
        session_clause = ""
        if session_id is not None:
            session_clause = " AND s.session_id=?"
            params.append(session_id)
        row = query_one(
            f"""
            SELECT s.*,
                   COALESCE(
                       s.detected_at, s.updated_at, mr.completed_at, s.created_at
                   ) AS state_detected_at
            FROM collaboration_state_segments AS s
            LEFT JOIN monitor_runs AS mr ON mr.id=s.source_run_id
            WHERE s.group_id=?
              {session_clause}
              AND s.source='state_monitor'
              AND s.assessment_status='confirmed'
            ORDER BY COALESCE(
                         s.detected_at, s.updated_at, mr.completed_at, s.created_at
                     ) DESC,
                     COALESCE(s.end_sequence, s.end_message_id, 0) DESC,
                     s.id DESC
            LIMIT 1
            """,
            tuple(params),
        )
        if not row:
            return None
        return EmotionReflectionService._state_from_segment(
            row,
            state_source="state_monitor",
            detected_at=row["state_detected_at"],
        )

    @staticmethod
    def _get_latest_batch_state(
        group_id, session_id=None, discussion_id=None
    ):
        where = ["group_id=?", "status='succeeded'"]
        params = [group_id]
        if session_id is not None:
            where.append("session_id=?")
            params.append(session_id)
        if discussion_id is not None:
            where.append("discussion_id=?")
            params.append(discussion_id)
        batch = query_one(
            f"""
            SELECT * FROM state_assessment_batches
            WHERE {' AND '.join(where)}
            ORDER BY completed_at DESC, id DESC
            LIMIT 1
            """,
            tuple(params),
        )
        if not batch:
            return None
        segments = query_all(
            """
            SELECT * FROM collaboration_state_segments
            WHERE assessment_batch_id=?
              AND source='llm'
              AND assessment_status='confirmed'
            ORDER BY segment_order ASC, id ASC
            """,
            (batch["id"],),
        )
        active_row = next(
            (
                row for row in reversed(segments)
                if int(row["is_active_at_batch_end"] or 0) == 1
            ),
            None,
        )
        if active_row:
            result = EmotionReflectionService._state_from_segment(
                active_row,
                state_source="assessment_batch",
                detected_at=batch["completed_at"],
            )
        else:
            result = {
                "state_code": "unknown",
                "state_label": STATE_META["unknown"][0],
                "source": "assessment_batch",
                "confidence": None,
                "detected_at": batch["completed_at"],
                "evidence_sequences": [],
                "evidence_end_sequence": None,
                "segment_id": None,
            }
        inactive_risk_states = []
        inactive_risk_evidence = []
        for row in segments:
            state_code = EmotionReflectionService._canonical_state_code(
                row["state_code"]
            )
            if (
                state_code in RISK_STATE_CODES
                and int(row["is_active_at_batch_end"] or 0) == 0
            ):
                inactive_risk_states.append(state_code)
                risk_segment = EmotionReflectionService._state_from_segment(
                    row,
                    state_source="assessment_batch",
                    detected_at=batch["completed_at"],
                )
                inactive_risk_evidence.extend(
                    risk_segment.get("evidence_sequences") or []
                )
        result.update({
            "assessment_batch_id": int(batch["id"]),
            "candidate_end_sequence": int(batch["candidate_end_sequence"]),
            "inactive_risk_states": sorted(set(inactive_risk_states)),
            "inactive_risk_evidence_sequences": (
                EmotionReflectionService._parse_int_list(inactive_risk_evidence)
            ),
        })
        return result

    @staticmethod
    def _state_is_newer(left, right):
        if not left:
            return False
        if not right:
            return True
        left_sequence = left.get("evidence_end_sequence")
        right_sequence = (
            right.get("candidate_end_sequence")
            if right.get("source") == "assessment_batch"
            else right.get("evidence_end_sequence")
        )
        if left_sequence is not None and right_sequence is not None:
            if int(left_sequence) != int(right_sequence):
                return int(left_sequence) > int(right_sequence)
        left_at = EmotionReflectionService._parse_datetime(left.get("detected_at"))
        right_at = EmotionReflectionService._parse_datetime(right.get("detected_at"))
        if left_at and right_at:
            if left_at.tzinfo is not None and right_at.tzinfo is None:
                right_at = right_at.replace(tzinfo=left_at.tzinfo)
            elif left_at.tzinfo is None and right_at.tzinfo is not None:
                left_at = left_at.replace(tzinfo=right_at.tzinfo)
            return left_at > right_at
        return bool(left_at and not right_at)

    @staticmethod
    def _get_latest_dominant_state(
        group_id,
        session_id=None,
        discussion_id=None,
        current_time=None,
        freshness_seconds=STATE_FRESHNESS_SECONDS,
    ):
        """Fuse the newest monitor risk with the latest completed batch state."""
        monitor = EmotionReflectionService._get_latest_monitor_state(
            group_id, session_id=session_id
        )
        batch = EmotionReflectionService._get_latest_batch_state(
            group_id,
            session_id=session_id,
            discussion_id=discussion_id,
        )

        batch_recovers_monitor = False
        if (
            batch
            and batch.get("state_code") == "positive_collaboration"
            and monitor
            and monitor.get("state_code") in RISK_STATE_CODES
        ):
            monitor_end = monitor.get("evidence_end_sequence")
            batch_end = batch.get("candidate_end_sequence")
            batch_recovers_monitor = (
                monitor_end is None
                or batch_end is None
                or int(batch_end) >= int(monitor_end)
            ) and not EmotionReflectionService._state_is_newer(monitor, batch)

        batch_has_explicit_recovery = bool(
            batch
            and batch.get("state_code") == "positive_collaboration"
            and batch.get("inactive_risk_states")
        )
        state_has_recovered = bool(
            batch_has_explicit_recovery or batch_recovers_monitor
        )

        if (
            monitor
            and monitor.get("state_code") in RISK_STATE_CODES
            and EmotionReflectionService._state_is_newer(monitor, batch)
        ):
            selected = monitor
            state_has_recovered = False
        elif batch:
            selected = batch
        else:
            selected = monitor

        recovery_evidence = []
        if state_has_recovered and batch:
            recovery_evidence = list(batch.get("evidence_sequences") or [])
        if selected:
            dominant = {
                "state_code": selected.get("state_code") or "unknown",
                "state_label": selected.get("state_label") or STATE_META["unknown"][0],
                "state_source": selected.get("source") or "none",
                "detected_at": selected.get("detected_at"),
                "evidence_sequences": list(
                    selected.get("evidence_sequences") or []
                ),
                "freshness_seconds": EmotionReflectionService._freshness_seconds(
                    selected.get("detected_at"), current_time=current_time
                ),
                "state_has_recovered": state_has_recovered,
                "recovery_evidence_sequences": recovery_evidence,
            }
        else:
            dominant = {
                "state_code": "unknown",
                "state_label": STATE_META["unknown"][0],
                "state_source": "none",
                "detected_at": None,
                "evidence_sequences": [],
                "freshness_seconds": None,
                "state_has_recovered": False,
                "recovery_evidence_sequences": [],
            }
            state_has_recovered = False

        freshness = dominant.get("freshness_seconds")
        if freshness is not None and freshness > int(freshness_seconds):
            dominant.update({
                "stale_state_code": dominant["state_code"],
                "state_code": "unknown",
                "state_label": STATE_META["unknown"][0],
                "state_source": "stale",
                "state_has_recovered": False,
                "recovery_evidence_sequences": [],
            })
            state_has_recovered = False
            recovery_evidence = []

        return {
            "latest_monitor_state": monitor,
            "latest_batch_state": batch,
            "dominant_state": dominant,
            "state_has_recovered": state_has_recovered,
            "recovery_evidence_sequences": recovery_evidence,
        }

    @staticmethod
    def _get_latest_group_state(group_id, session_id=None, discussion_id=None):
        if session_id is not None and discussion_id is not None:
            cursor = query_one(
                """
                SELECT observation_status
                FROM discussion_assessment_cursors
                WHERE group_id=? AND session_id=? AND discussion_id=?
                """,
                (group_id, session_id, discussion_id),
            )
            if cursor and cursor["observation_status"] == "observing":
                return {
                    "state_code": "observing",
                    "state_label": "观察中",
                    "assessment_status": "observing",
                }
            confirmed = query_one(
                """
                SELECT s.state_code, s.confidence, b.completed_at
                FROM collaboration_state_segments AS s
                JOIN state_assessment_batches AS b ON b.id=s.assessment_batch_id
                WHERE s.group_id=? AND s.session_id=? AND b.discussion_id=?
                  AND s.source='llm' AND s.assessment_status='confirmed'
                  AND b.status='succeeded'
                  AND COALESCE(s.is_active_at_batch_end, 0)=1
                ORDER BY b.completed_at DESC, s.segment_order DESC, s.id DESC
                LIMIT 1
                """,
                (group_id, session_id, discussion_id),
            )
            if confirmed:
                data = dict(confirmed)
                data["assessment_status"] = "confirmed"
                return data
        if session_id is not None:
            row = query_one(
                """
                SELECT * FROM group_states
                WHERE group_id=? AND session_id=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (group_id, session_id),
            )
        else:
            row = query_one(
                """
                SELECT * FROM group_states
                WHERE group_id=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (group_id,),
            )
        return dict(row) if row else None

    @staticmethod
    def _get_checkin_summary(group_id, window_start, window_end, session_id=None):
        if session_id is None:
            rows = query_all(
                """SELECT emotion_option, COUNT(*) AS cnt
                   FROM emotion_checkins
                   WHERE group_id=? AND created_at>=? AND created_at<=?
                   GROUP BY emotion_option ORDER BY cnt DESC""",
                (group_id, window_start, window_end),
            )
        else:
            rows = query_all(
                """SELECT emotion_option, COUNT(*) AS cnt
                   FROM emotion_checkins
                   WHERE group_id=? AND session_id=?
                     AND created_at>=? AND created_at<=?
                   GROUP BY emotion_option ORDER BY cnt DESC""",
                (group_id, session_id, window_start, window_end),
            )
        if not rows:
            return "无情绪签到数据"
        parts = [r["emotion_option"] + "(" + str(r["cnt"]) + "人)" for r in rows]
        return "\uff0c".join(parts)

    @staticmethod
    def _build_message_summary(messages):
        if not messages:
            return "暂无新消息"
        return "最近" + str(len(messages)) + "条讨论消息"

    @staticmethod
    def _build_interaction_summary(messages):
        count = len(messages)
        if count == 0:
            return "尚未开始讨论"
        elif count <= 3:
            return "讨论较为安静"
        elif count <= 8:
            return "讨论正常进行中"
        else:
            return "讨论较为活跃"

    @staticmethod
    def _build_state_summary(latest_state):
        if not latest_state:
            return "暂无评估"
        label = latest_state.get("state_label") or latest_state.get("state_code") or ""
        risk = latest_state.get("risk_label") or ""
        if risk:
            return label + "\uff08" + risk + "\uff09"
        return label

    @staticmethod
    def _get_fallback(state_code="unknown", previous_message=None):
        feedback_config = EMOTION_FEEDBACK_TYPES.get(str(state_code or ""))
        if feedback_config:
            candidates = [text for _template_id, text in feedback_config["templates"]]
        else:
            canonical = EmotionReflectionService._canonical_state_code(state_code)
            candidates = list(
                FALLBACK_MESSAGES.get(canonical)
                or FALLBACK_MESSAGES["unknown"]
            )
        previous_text = (
            previous_message.get("content")
            if isinstance(previous_message, dict)
            else previous_message
        )
        if previous_text:
            distinct = [
                message
                for message in candidates
                if not EmotionReflectionService._messages_are_too_similar(
                    message, previous_text
                )
            ]
            if distinct:
                candidates = distinct
            else:
                return None
        return random.choice(candidates) if candidates else None

    @staticmethod
    def _build_validation_audit(
        model_validation,
        *,
        fallback_validation=None,
        final_passed=False,
    ):
        model_validation = model_validation or {
            "valid": False,
            "reason": "missing_validation",
            "failure_codes": ["missing_validation"],
            "checks": {},
        }
        failures = list(model_validation.get("failure_codes") or [])
        if fallback_validation:
            failures.extend(fallback_validation.get("failure_codes") or [])
        final_validation = fallback_validation or model_validation
        checks = dict(final_validation.get("checks") or {})
        structure_only = checks.get("validation_scope") == "structure_only"
        return {
            "non_empty": checks.get("non_empty", False),
            "single_sentence": checks.get("single_sentence", None if structure_only else False),
            "length_ok": checks.get("length_ok", None if structure_only else False),
            "has_emoji": checks.get("has_emoji", None if structure_only else False),
            "no_suggestion": checks.get("no_suggestion", None if structure_only else False),
            "no_personal": checks.get("no_personal", None if structure_only else False),
            "state_consistent": checks.get("state_consistent", False),
            "not_duplicate": checks.get("not_duplicate", None if structure_only else False),
            "validation_scope": checks.get("validation_scope"),
            "content_checked": checks.get("content_checked"),
            "length_checked": checks.get("length_checked"),
            "final_passed": bool(final_passed),
            "fallback_reason": (
                None
                if final_passed and not fallback_validation
                else final_validation.get("reason")
            ),
            "validation_failure_codes": list(dict.fromkeys(failures)),
            "model_validation": model_validation,
            "fallback_validation": fallback_validation,
        }

    @staticmethod
    def _build_emotion_audit(
        *,
        context,
        slot_id,
        model_raw_message,
        validation_result,
        fallback_state_code,
        fallback_message,
        final_visible_message,
        final_disposition,
        classification,
        reference_templates,
    ):
        recent_messages = (
            context.get("recent_student_messages")
            or context.get("recent_messages")
            or []
        )
        sequences = EmotionReflectionService._parse_int_list([
            item.get("sequence")
            for item in recent_messages
            if isinstance(item, dict)
        ])
        dominant = (
            context.get("dominant_state")
            or context.get("latest_group_state")
            or {}
        )
        previous = context.get("previous_emotion_message") or {}
        classification = classification or {}
        classification_record = {
            key: value
            for key, value in classification.items()
            if key != "prompt_data"
        }
        reference_templates = reference_templates or []
        feedback_output = {
            "schema_version": EMOTION_FEEDBACK_SCHEMA_VERSION,
            "feedback_type_code": classification.get("feedback_type_code"),
            "feedback_type_label": classification.get("feedback_type_label"),
            "content": final_visible_message,
        }
        return {
            "slot_id": slot_id,
            "context_student_sequence_start": (
                min(sequences) if sequences else None
            ),
            "context_student_sequence_end": (
                max(sequences) if sequences else None
            ),
            "context_student_sequences": sequences,
            "latest_monitor_state": context.get("latest_monitor_state"),
            "latest_batch_state": context.get("latest_batch_state"),
            "dominant_state": EmotionReflectionService._canonical_state_code(
                dominant.get("state_code")
            ),
            "dominant_state_source": (
                dominant.get("state_source")
                or dominant.get("source")
                or "none"
            ),
            "state_freshness_seconds": dominant.get("freshness_seconds"),
            "state_has_recovered": bool(
                context.get(
                    "state_has_recovered",
                    dominant.get("state_has_recovered", False),
                )
            ),
            "previous_emotion_run_id": (
                context.get("previous_emotion_run_id")
                or (
                    previous.get("run_id")
                    if isinstance(previous, dict)
                    else None
                )
            ),
            "model_raw_message": model_raw_message,
            "validation_result": validation_result,
            "validation_failure_codes": list(
                validation_result.get("validation_failure_codes") or []
            ),
            "fallback_state_code": fallback_state_code,
            "fallback_message": fallback_message,
            "final_visible_message": final_visible_message,
            "final_disposition": final_disposition,
            "emotion_feedback_schema_version": EMOTION_FEEDBACK_SCHEMA_VERSION,
            "emotion_feedback_type_code": classification.get(
                "feedback_type_code"
            ),
            "emotion_feedback_type_label": classification.get(
                "feedback_type_label"
            ),
            "emotion_feedback_classification": classification_record,
            "emotion_reference_template_ids": [
                item.get("template_id")
                for item in reference_templates
                if isinstance(item, dict) and item.get("template_id")
            ],
            "emotion_feedback_output": feedback_output,
        }

    @staticmethod
    def _create_intervention_run(
        group_id, status, generated_message, fallback_used,
        context, prompt_data, llm_result, validation_result,
        teacher_config_json, window_start, window_end,
        scheduled_at, actual_started_at,
        session_id=None, session_no=None, discussion_id=None, task_id=None,
        tick_index=None, slot_id=None, skip_reason=None,
        emotion_audit=None,
    ):
        ctx_json = json.dumps(context, ensure_ascii=False, default=str) if context else None
        # Limit context snapshot to prevent DB bloat (max ~2KB)
        if ctx_json and len(ctx_json) > 2000:
            if context and isinstance(context, dict):
                ctx_copy = dict(context)
                if "recent_messages" in ctx_copy:
                    msgs = ctx_copy["recent_messages"]
                    if isinstance(msgs, list):
                        ctx_copy["recent_messages"] = [
                            {k: (v[:120] + "..." if k == "content" and isinstance(v, str) and len(v) > 120 else v)
                             for k, v in m.items()}
                            for m in msgs[:8]
                        ]
                        ctx_copy["message_count"] = len(ctx_copy["recent_messages"])
                ctx_json = json.dumps(ctx_copy, ensure_ascii=False, default=str)
                if len(ctx_json) > 2000:
                    ctx_json = ctx_json[:2000] + "...[truncated]"
            else:
                ctx_json = ctx_json[:2000] + "...[truncated]"
        prompt_json = json.dumps(prompt_data, ensure_ascii=False) if prompt_data else None

        if hasattr(llm_result, "to_dict"):
            resp = json.dumps(llm_result.to_dict(), ensure_ascii=False)
        else:
            resp = json.dumps({
                "success": llm_result.success, "output": llm_result.output,
                "raw_text": llm_result.raw_text,
                "failure_type": llm_result.failure_type,
                "latency_ms": llm_result.latency_ms,
            }, ensure_ascii=False)

        val_json = EmotionReflectionService._build_validation_json(validation_result)
        emotion_audit = emotion_audit or {}
        emotion_audit_json = json.dumps(
            emotion_audit, ensure_ascii=False, default=str
        )

        frozen_sequences = EmotionReflectionService._parse_int_list(
            (context or {}).get("frozen_input_sequences")
        )
        if (context or {}).get("window_frozen"):
            cutoff_sequence = max(frozen_sequences) if frozen_sequences else 0
        else:
            cutoff_row = query_one(
                """
                SELECT COALESCE(MAX(sequence), 0) AS cutoff_sequence
                FROM messages
                WHERE group_id=? AND (? IS NULL OR session_id=?)
                  AND NOT (role='agent' AND COALESCE(agent_type, '')='emotion')
                """,
                (group_id, session_id, session_id),
            )
            cutoff_sequence = int(
                cutoff_row["cutoff_sequence"] if cutoff_row else 0
            )

        try:
            return execute(
                """INSERT INTO intervention_runs(
                group_id, session_id, session_no, discussion_id, task_id, cutoff_sequence,
                agent_type, trigger_type, strategy_id, status,
                generated_message, fallback_used, skip_reason,
                llm_context_json, llm_prompt_json, llm_response_json,
                validation_json, teacher_config_snapshot_json,
                window_start, window_end, scheduled_at,
                actual_started_at, created_at, tick_index, metadata_json,
                emotion_slot_id,
                context_student_sequence_start,
                context_student_sequence_end,
                context_student_sequences_json,
                latest_monitor_state_json,
                latest_batch_state_json,
                dominant_state,
                dominant_state_source,
                state_freshness_seconds,
                state_has_recovered,
                previous_emotion_run_id,
                model_raw_message,
                validation_failure_codes_json,
                fallback_state_code,
                fallback_message,
                final_visible_message,
                final_disposition,
                emotion_audit_json,
                emotion_feedback_schema_version,
                emotion_feedback_type_code,
                emotion_feedback_type_label,
                emotion_feedback_classification_json,
                emotion_reference_template_ids_json,
                emotion_feedback_output_json,
                validation_result
            ) VALUES(
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )""",
                (
                 group_id, session_id, session_no, discussion_id, task_id,
                 cutoff_sequence,
                 "emotion", "emotion_time_slot",
                 "emotion_reflection_slot", status,
                 generated_message, 1 if fallback_used else 0, skip_reason,
                 ctx_json, prompt_json, resp,
                 val_json, teacher_config_json,
                 window_start, window_end, scheduled_at,
                 actual_started_at, actual_started_at, tick_index,
                 json.dumps(
                     {
                         "emotion_slot_id": slot_id,
                         "slot_index": tick_index,
                         "emotion_audit": emotion_audit,
                     },
                     ensure_ascii=False,
                     default=str,
                 ),
                 slot_id,
                 emotion_audit.get("context_student_sequence_start"),
                 emotion_audit.get("context_student_sequence_end"),
                 json.dumps(
                     emotion_audit.get("context_student_sequences") or [],
                     ensure_ascii=False,
                 ),
                 json.dumps(
                     emotion_audit.get("latest_monitor_state"),
                     ensure_ascii=False,
                     default=str,
                 ),
                 json.dumps(
                     emotion_audit.get("latest_batch_state"),
                     ensure_ascii=False,
                     default=str,
                 ),
                 emotion_audit.get("dominant_state"),
                 emotion_audit.get("dominant_state_source"),
                 emotion_audit.get("state_freshness_seconds"),
                 1 if emotion_audit.get("state_has_recovered") else 0,
                 emotion_audit.get("previous_emotion_run_id"),
                 emotion_audit.get("model_raw_message"),
                 json.dumps(
                     emotion_audit.get("validation_failure_codes") or [],
                     ensure_ascii=False,
                 ),
                 emotion_audit.get("fallback_state_code"),
                 emotion_audit.get("fallback_message"),
                  emotion_audit.get("final_visible_message"),
                  emotion_audit.get("final_disposition"),
                  emotion_audit_json,
                  emotion_audit.get("emotion_feedback_schema_version"),
                  emotion_audit.get("emotion_feedback_type_code"),
                  emotion_audit.get("emotion_feedback_type_label"),
                  json.dumps(
                      emotion_audit.get("emotion_feedback_classification") or {},
                      ensure_ascii=False,
                      default=str,
                  ),
                  json.dumps(
                      emotion_audit.get("emotion_reference_template_ids") or [],
                      ensure_ascii=False,
                  ),
                  json.dumps(
                      emotion_audit.get("emotion_feedback_output") or {},
                      ensure_ascii=False,
                      default=str,
                  ),
                  "passed" if validation_result.get("final_passed") else "failed",
                  ),
            )
        except sqlite3.IntegrityError:
            existing = query_one(
                """
                SELECT id FROM intervention_runs
                WHERE emotion_slot_id=?
                  AND status NOT IN ('CANCELLED', 'FAILED', 'EXPIRED', 'STALE')
                ORDER BY id DESC
                LIMIT 1
                """,
                (slot_id,),
            ) if slot_id is not None else None
            if existing:
                return int(existing["id"])
            existing = query_one(
                """
                SELECT id FROM intervention_runs
                WHERE group_id=?
                  AND COALESCE(session_id, 0)=COALESCE(?, 0)
                  AND COALESCE(cutoff_sequence, 0)=?
                  AND COALESCE(agent_type, '')='emotion'
                  AND COALESCE(trigger_type, '')='emotion_time_slot'
                  AND status NOT IN ('CANCELLED', 'FAILED', 'EXPIRED', 'STALE')
                ORDER BY id DESC
                LIMIT 1
                """,
                (group_id, session_id, cutoff_sequence),
            )
            if existing:
                return int(existing["id"])
            raise

    @staticmethod
    def _publish_emotion_message(
        *, group_id, session_id, discussion_id, task_id, session_no,
        message, intervention_run_id, emotion_slot_id=None,
        expected_student_sequence=None, fallback_used=False,
        feedback_metadata=None,
    ):
        """Apply the final coordination gate and publish in one transaction."""
        agent_user_id = get_sera_user_id()
        if not agent_user_id:
            return {"ok": False, "reason": "sera_user_not_found"}
        now = now_str()
        cmid = "emotion-slot-" + str(intervention_run_id)
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")

            def blocked(
                reason,
                slot_status,
                *,
                run_status=None,
                final_disposition=None,
                strategy_run_id=None,
                superseded_by_slot_id=None,
            ):
                conn.rollback()
                return {
                    "ok": False,
                    "reason": reason,
                    "slot_status": slot_status,
                    "run_status": run_status,
                    "final_disposition": final_disposition or reason,
                    "strategy_run_id": strategy_run_id,
                    "superseded_by_slot_id": superseded_by_slot_id,
                }

            run = conn.execute(
                "SELECT id, message_id FROM intervention_runs WHERE id=? AND group_id=?",
                (intervention_run_id, group_id),
            ).fetchone()
            if not run:
                return blocked("intervention_run_not_found", "failed")
            if run["message_id"]:
                conn.commit()
                return {"ok": True, "duplicate": True, "message_id": int(run["message_id"])}

            if emotion_slot_id is not None:
                existing_slot_message = conn.execute(
                    """
                    SELECT m.id
                    FROM messages AS m
                    JOIN intervention_runs AS ir ON ir.id=m.intervention_run_id
                    WHERE ir.emotion_slot_id=?
                      AND COALESCE(m.agent_type, '')='emotion'
                    ORDER BY m.id ASC LIMIT 1
                    """,
                    (int(emotion_slot_id),),
                ).fetchone()
                if existing_slot_message:
                    conn.commit()
                    return {
                        "ok": True,
                        "duplicate": True,
                        "message_id": int(existing_slot_message["id"]),
                    }

            scope = conn.execute(
                """
                SELECT gsd.status AS discussion_status,
                       es.status AS session_status,
                       gsd.submitted_at, gsd.auto_submitted_at, gsd.deadline,
                       es.task_id AS session_task_id,
                       es.agent_mode,
                        g.state AS group_state,
                       COALESCE((
                           SELECT agent_paused
                           FROM group_session_controls
                           WHERE group_id=gsd.group_id AND session_id=gsd.session_id
                           ORDER BY id DESC LIMIT 1
                       ), 0) AS agent_paused
                FROM group_session_discussions AS gsd
                JOIN experiment_sessions AS es ON es.id=gsd.session_id
                JOIN groups AS g ON g.id=gsd.group_id
                WHERE gsd.id=? AND gsd.group_id=? AND gsd.session_id=?
                """,
                (discussion_id, group_id, session_id),
            ).fetchone()
            if (
                not scope
                or scope["discussion_status"] != "running"
                or scope["session_status"] != "running"
                or scope["submitted_at"]
                or scope["auto_submitted_at"]
                or (scope["deadline"] and scope["deadline"] <= now)
            ):
                return blocked(
                    "discussion_closed_after_generation",
                    "expired",
                    run_status="EXPIRED",
                )
            if (
                scope["agent_mode"] != "emotion"
                or scope["group_state"] == "CLOSED"
                or bool(scope["agent_paused"])
                or (
                    task_id is not None
                    and scope["session_task_id"] is not None
                    and int(task_id) != int(scope["session_task_id"])
                )
            ):
                return blocked(
                    "emotion_scope_disabled_after_generation",
                    "expired",
                    run_status="EXPIRED",
                )

            if task_id is not None and session_no is not None:
                document = conn.execute(
                    """
                    SELECT status, submitted_at
                    FROM collaborative_documents
                    WHERE group_id=? AND task_id=? AND session_no=?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (group_id, task_id, session_no),
                ).fetchone()
                if document and (
                    document["submitted_at"] is not None
                    or document["status"] in {
                        "submitted", "locked", "frozen", "closed", "submitting"
                    }
                ):
                    return blocked(
                        "document_closed_after_generation",
                        "expired",
                        run_status="EXPIRED",
                    )

            slot = None
            if emotion_slot_id is not None:
                slot = conn.execute(
                    """
                    SELECT id, slot_index, status
                    FROM emotion_reflection_slots
                    WHERE id=? AND group_id=? AND session_id=? AND discussion_id=?
                    """,
                    (emotion_slot_id, group_id, session_id, discussion_id),
                ).fetchone()
                if slot:
                    newer_slot = conn.execute(
                        """
                        SELECT id, slot_index
                        FROM emotion_reflection_slots
                        WHERE discussion_id=? AND slot_index>?
                        ORDER BY slot_index DESC, id DESC LIMIT 1
                        """,
                        (discussion_id, slot["slot_index"]),
                    ).fetchone()
                    if newer_slot:
                        return blocked(
                            "newer_slot_after_generation",
                            "superseded",
                            run_status="SUPERSEDED",
                            superseded_by_slot_id=int(newer_slot["id"]),
                        )
                    if slot["status"] != "running":
                        return blocked(
                            "slot_no_longer_current_after_generation",
                            "superseded",
                            run_status="SUPERSEDED",
                        )

            if slot and expected_student_sequence is not None:
                latest_student = conn.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) AS latest_sequence
                    FROM messages
                    WHERE group_id=? AND session_id=? AND discussion_id=?
                      AND role='student'
                    """,
                    (group_id, session_id, discussion_id),
                ).fetchone()
                latest_sequence = int(latest_student["latest_sequence"] or 0)
                if latest_sequence != int(expected_student_sequence):
                    return blocked(
                        "student_context_changed_after_generation",
                        "suppressed",
                        run_status="STALE",
                    )

            if slot:
                recent_emotion_cutoff = (
                    datetime.now()
                    - timedelta(seconds=max(1, int(EMOTION_INTERVAL_SECONDS or 300)))
                ).strftime("%Y-%m-%d %H:%M:%S")
                recent_emotion = conn.execute(
                    """
                    SELECT m.id
                    FROM messages AS m
                    JOIN intervention_runs AS ir ON ir.id=m.intervention_run_id
                    WHERE m.group_id=? AND m.session_id=? AND m.discussion_id=?
                      AND COALESCE(m.agent_type, '')='emotion'
                      AND ir.emotion_slot_id<>?
                      AND m.created_at>?
                    ORDER BY m.created_at DESC, m.id DESC LIMIT 1
                    """,
                    (
                        group_id,
                        session_id,
                        discussion_id,
                        emotion_slot_id,
                        recent_emotion_cutoff,
                    ),
                ).fetchone()
                if recent_emotion:
                    return blocked(
                        "recent_emotion_publish",
                        "deferred",
                        run_status="STALE",
                    )

            existing = conn.execute(
                """
                SELECT id FROM messages
                WHERE group_id=? AND user_id=? AND client_message_id=?
                ORDER BY id ASC LIMIT 1
                """,
                (group_id, agent_user_id, cmid),
            ).fetchone()
            if existing:
                message_id = int(existing["id"])
            else:
                conn.execute(
                    """
                    UPDATE groups
                       SET last_message_sequence=MAX(
                           COALESCE(last_message_sequence, 0),
                           COALESCE((SELECT MAX(sequence) FROM messages WHERE group_id=?), 0)
                       ) + 1
                     WHERE id=?
                    """,
                    (group_id, group_id),
                )
                sequence_row = conn.execute(
                    "SELECT last_message_sequence FROM groups WHERE id=?", (group_id,)
                ).fetchone()
                sequence = sequence_row["last_message_sequence"] if sequence_row else None
                cur = conn.execute(
                    """
                    INSERT INTO messages(
                        group_id, user_id, content, role, sender_type,
                        client_message_id, intervention_run_id, sequence,
                        created_at, session_no, task_id, session_id,
                        agent_type, trigger_source, metadata_json,
                        discussion_id, scope_resolved_from,
                        legacy_scope_fallback, scope_fallback_reason
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        group_id, agent_user_id, message, "agent", "agent",
                        cmid, intervention_run_id, sequence, now, session_no,
                        task_id, session_id, "emotion", "emotion_schedule",
                        json.dumps(
                            {
                                "discussion_id": discussion_id,
                                "emotion_slot": True,
                                **(feedback_metadata or {}),
                            },
                            ensure_ascii=False,
                        ),
                        discussion_id,
                        "session",
                        0,
                        None,
                    ),
                )
                message_id = int(cur.lastrowid)

            status = "FALLBACK" if fallback_used else "PUBLISHED"
            cur = conn.execute(
                """
                UPDATE intervention_runs
                   SET status=?, decision='INTERVENE', message_id=?,
                       session_id=?, discussion_id=?, task_id=?,
                       agent_type='emotion', trigger_type='emotion_time_slot',
                       actual_published_at=?, published_at=?, completed_at=?
                 WHERE id=? AND group_id=? AND message_id IS NULL
                """,
                (
                    status, message_id, session_id, discussion_id, task_id,
                    now, now, now, intervention_run_id, group_id,
                ),
            )
            if cur.rowcount != 1:
                raise RuntimeError("intervention run message backfill failed")
            conn.commit()
            return {"ok": True, "message_id": message_id, "status": status}
        except Exception as exc:
            conn.rollback()
            logger.exception("emotion message publish failed run_id=%s", intervention_run_id)
            return {"ok": False, "reason": "publish_failed", "error": str(exc)}
        finally:
            conn.close()

    @staticmethod
    def _build_validation_json(validation_result):
        try:
            checks = validation_result.get("checks", {})
            return json.dumps({
                "non_empty": validation_result.get(
                    "non_empty", checks.get("non_empty", False)
                ),
                "single_sentence": validation_result.get(
                    "single_sentence", checks.get("single_sentence", False)
                ),
                "length_ok": validation_result.get(
                    "length_ok", checks.get("length_ok", False)
                ),
                "length_checked": validation_result.get(
                    "length_checked", checks.get("length_checked")
                ),
                "content_checked": validation_result.get(
                    "content_checked", checks.get("content_checked")
                ),
                "validation_scope": validation_result.get(
                    "validation_scope", checks.get("validation_scope")
                ),
                "has_emoji": validation_result.get(
                    "has_emoji", checks.get("has_emoji", False)
                ),
                "no_suggestion": validation_result.get(
                    "no_suggestion", checks.get("no_suggestion", False)
                ),
                "no_personal": validation_result.get(
                    "no_personal", checks.get("no_personal", False)
                ),
                "state_consistent": validation_result.get(
                    "state_consistent",
                    checks.get("state_consistent", False),
                ),
                "not_duplicate": validation_result.get(
                    "not_duplicate", checks.get("not_duplicate", False)
                ),
                "emoji_count_ok": validation_result.get(
                    "emoji_count_ok", checks.get("emoji_count_ok", False)
                ),
                "group_addressed": validation_result.get(
                    "group_addressed", checks.get("group_addressed", False)
                ),
                "no_pressure_language": validation_result.get(
                    "no_pressure_language",
                    checks.get("no_pressure_language", False),
                ),
                "feedback_type_consistent": validation_result.get(
                    "feedback_type_consistent",
                    checks.get("feedback_type_consistent", False),
                ),
                "final_passed": validation_result.get(
                    "final_passed",
                    validation_result.get("valid", False),
                ),
                "fallback_reason": validation_result.get(
                    "fallback_reason",
                    validation_result.get("reason"),
                ),
                "validation_failure_codes": (
                    validation_result.get("validation_failure_codes")
                    or validation_result.get("failure_codes")
                    or []
                ),
                "model_validation": validation_result.get(
                    "model_validation"
                ),
                "fallback_validation": validation_result.get(
                    "fallback_validation"
                ),
            }, ensure_ascii=False)
        except Exception:
            return None
