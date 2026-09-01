# -*- coding: utf-8 -*-
"""Phase 7 optional LLM analyzer with anonymization and strict JSON validation.

Refactored to use LLMGateway internally.  The old function signatures are preserved
as compatibility adapters.

Refactored to use LLMGateway internally.  The old function signatures are preserved
as compatibility adapters."""
import json
import logging
import time
from datetime import datetime

from knowledge_base import FINAL_STATE_CODES


logger = logging.getLogger(__name__)


try:
    from services.llm_gateway import get_gateway, AUTHENTICATION_ERROR, RATE_LIMITED, CONNECT_TIMEOUT, READ_TIMEOUT, NETWORK_ERROR, UPSTREAM_5XX, INVALID_RESPONSE, UNKNOWN_ERROR
except ImportError:
    pass


logger = logging.getLogger(__name__)


try:
    from services.llm_gateway import get_gateway, AUTHENTICATION_ERROR, RATE_LIMITED, CONNECT_TIMEOUT, READ_TIMEOUT, NETWORK_ERROR, UPSTREAM_5XX, INVALID_RESPONSE, UNKNOWN_ERROR
except ImportError:
    pass


from config import (
    SEMANTIC_ANALYSIS_MIN_MESSAGES,
    SERA_LLM_BASE_URL,
    SERA_LLM_MAX_CONTEXT,
    SERA_LLM_MODEL,
    SERA_LLM_TIMEOUT,
    PIPELINE_V2_LLM_MAX_JSON_RETRIES,
    USE_LLM_ANALYSIS,
)

LLM_ANALYZER_PROMPT_VERSION = "phase7_llm_v1"
ALLOWED_PRIMARY_STATES = set(FINAL_STATE_CODES)
ALLOWED_ASSESSMENT_STATUS = {
    "confirmed",
    "uncertain",
    "insufficient_evidence",
}


def _llm_enabled():
    return bool(USE_LLM_ANALYSIS and SERA_LLM_BASE_URL and SERA_LLM_MODEL)


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _speaker_alias(index):
    if index < 26:
        return f"学生{chr(ord('A') + index)}"
    return f"学生{index + 1}"


def _compact_text(value, limit):
    return str(value or "").strip().replace("\r", " ").replace("\n", " ")[:limit]


def _coerce_list(value, *, item_limit=12, text_limit=120):
    if not isinstance(value, list):
        return []
    items = []
    for item in value[:item_limit]:
        text = _compact_text(item, text_limit)
        if text:
            items.append(text)
    return items


def _extract_json_payload(raw_text):
    outer = json.loads(raw_text)
    if isinstance(outer, dict) and outer.get("choices"):
        message = outer["choices"][0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, list):
            content = "".join(block.get("text", "") for block in content if isinstance(block, dict))
        # DeepSeek reasoning model: content may be empty, reasoning in reasoning_content
        if not content or not str(content).strip():
            reasoning = message.get("reasoning_content", "")
            if reasoning:
                content = reasoning
        if isinstance(content, dict):
            return content
        return json.loads(content)
    if isinstance(outer, dict):
        return outer
    raise ValueError("response_not_json_object")


def _extract_chat_text(raw_text):
    """Extract plain text content from a chat/completions API response.
    Unlike _extract_json_payload, this does NOT try to JSON-parse the content.
    Used by intervention/help message generation where response is plain text."""
    outer = json.loads(raw_text)
    if isinstance(outer, dict) and outer.get("choices"):
        message = outer["choices"][0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, list):
            content = "".join(block.get("text", "") for block in content if isinstance(block, dict))
        # DeepSeek V4 Flash reasoning model: content may be empty, reasoning in reasoning_content
        # Some reasoning models return content in reasoning_content when content is empty
        if not content or not str(content).strip():
            reasoning = message.get("reasoning_content", "")
            if reasoning:
                content = reasoning
        return str(content)
    if isinstance(outer, dict):
        return str(outer)
    raise ValueError("response_not_chat_completion")

def _participant_alias_map(context):
    alias_map = {}
    for participant in context.get("participants") or []:
        user_id = participant.get("user_id")
        if user_id is None or user_id in alias_map:
            continue
        alias_map[user_id] = _speaker_alias(len(alias_map))
    for row in context.get("recent_student_messages") or []:
        user_id = row.get("user_id")
        if user_id is None or user_id in alias_map:
            continue
        alias_map[user_id] = _speaker_alias(len(alias_map))
    return alias_map


def anonymize_context_for_llm(context):
    alias_map = _participant_alias_map(context)
    recent_rows = (context.get("recent_student_messages") or [])[-max(8, SERA_LLM_MAX_CONTEXT):]
    anonymized_messages = []
    for row in recent_rows[-15:]:
        if (row.get("role") or "student") == "agent":
            continue
        user_id = row.get("user_id")
        anonymized_messages.append(
            {
                "speaker": alias_map.get(user_id, _speaker_alias(len(alias_map))),
                "content": _compact_text(row.get("content"), 400),
                "created_at": row.get("created_at"),
            }
        )

    participant_stats = []
    for participant in context.get("participants") or []:
        user_id = participant.get("user_id")
        participant_stats.append(
            {
                "speaker": alias_map.get(user_id, _speaker_alias(len(participant_stats))),
                "message_count_10m": int(participant.get("message_count_10m") or 0),
                "recent_message_count": int(participant.get("recent_message_count") or 0),
                "session_task_message_count": int(participant.get("session_task_message_count") or 0),
                "active_on_page": bool(participant.get("active_on_page")),
                "active_session_count": int(participant.get("active_session_count") or 0),
            }
        )

    progress = context.get("current_progress") or {}
    latest_submission = progress.get("latest_submission") or {}
    return {
        "recent_student_messages": anonymized_messages,
        "participant_stats": participant_stats,
        "context_summary": {
            "participant_count": int(context.get("participant_count") or 0),
            "window_minutes": int(context.get("window_minutes") or 0),
            "student_message_count_session": int(context.get("student_message_count_session") or 0),
            "active_member_count": int(context.get("active_member_count") or 0),
            "recent_intervention_exists": bool(context.get("recent_intervention")),
            "recent_help_request_exists": bool(context.get("recent_help_request")),
            "has_submission": bool(progress.get("has_submission")),
            "submission_count": int(progress.get("submission_count") or 0),
            "recent_submission_count": int(progress.get("recent_submission_count") or 0),
            "latest_submission_length": int(latest_submission.get("content_length") or 0),
        },
    }


def _sanitize_behavior_features(behavior_features, context):
    alias_map = _participant_alias_map(context)
    key_alias_map = {}
    for participant in context.get("participants") or []:
        alias = alias_map.get(participant.get("user_id"))
        for raw_key in (
            participant.get("participant_code"),
            participant.get("username"),
            participant.get("real_name"),
            str(participant.get("user_id")) if participant.get("user_id") is not None else None,
        ):
            if raw_key:
                key_alias_map[str(raw_key)] = alias

    sanitized = dict(behavior_features or {})
    for field in ("participation_distribution", "individual_silence_seconds"):
        values = sanitized.get(field)
        if not isinstance(values, dict):
            continue
        sanitized[field] = {
            key_alias_map.get(str(key), str(key)): value
            for key, value in values.items()
        }
    return sanitized


def build_llm_prompt_context(group_id, context, features=None, rule_result=None):
    anonymized = anonymize_context_for_llm(context)
    task = context.get("current_task") or {}
    rule_assessment = {}
    if isinstance(rule_result, dict):
        rule_assessment = rule_result.get("rule_assessment") or rule_result.get("rule_assessment_json") or {}

    candidates = []
    for candidate in (rule_assessment.get("candidates") or [])[:5]:
        candidates.append(
            {
                "state_code": candidate.get("state_code"),
                "score": round(_safe_float(candidate.get("score"), 0.0), 3),
                "signals": [
                    {
                        "reason": _compact_text(signal.get("reason"), 80),
                        "value": _compact_text(signal.get("value"), 120),
                    }
                    for signal in (candidate.get("signals") or [])[:4]
                    if isinstance(signal, dict)
                ],
            }
        )

    return {
        "group_id": group_id,
        "task": {
            "title": _compact_text(task.get("title"), 120),
            "question": _compact_text(task.get("question"), 300),
            "task_goal": _compact_text(task.get("task_goal"), 500),
            "expected_dimensions": _coerce_list(task.get("expected_dimensions"), item_limit=8, text_limit=80),
            "key_concepts": _coerce_list(task.get("key_concepts"), item_limit=10, text_limit=60),
            "output_requirement": _compact_text(task.get("output_requirement"), 300),
        },
        "session_no": context.get("session_no"),
        "task_id": context.get("task_id"),
        "window_start": context.get("window_start"),
        "window_end": context.get("window_end"),
        "recent_messages": anonymized["recent_student_messages"],
        "participant_statistics": anonymized["participant_stats"],
        "summary": anonymized["context_summary"],
        "behavior_features": _sanitize_behavior_features((features or {}).get("behavior") or {}, context),
        "text_features": (features or {}).get("text") or {},
        "rule_candidates": candidates,
        "rule_assessment_status": rule_assessment.get("assessment_status") or "insufficient_evidence",
        "valid_primary_states": sorted(ALLOWED_PRIMARY_STATES),
        "valid_assessment_status": sorted(ALLOWED_ASSESSMENT_STATUS),
    }


def _prompt_messages(prompt_context):
    system_prompt = (
        "你是协作学习状态分析器。"
        "只能根据匿名化的协作过程判断状态，不能给出任务答案。"
        "严格返回 JSON，字段必须完整，不能附加解释。"
    )
    user_prompt = {
        "prompt_version": LLM_ANALYZER_PROMPT_VERSION,
        "task": (
            "判断当前小组的主要状态。"
            "如果证据不足，assessment_status 必须为 insufficient_evidence。"
            "如观察到学生自发修复、回到任务或缓和冲突，self_regulation_detected 设为 true。"
        ),
        "required_json_schema": {
            "primary_state": "unknown",
            "secondary_flags": [],
            "confidence": 0.0,
            "assessment_status": "confirmed",
            "self_regulation_detected": False,
            "should_intervene_recommendation": False,
            "evidence_sentences": [],
            "reason": "",
        },
        "context": prompt_context,
    }
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
    ]


def _repair_prompt_messages(prompt_context, previous_output="", validation_error=""):
    messages = _prompt_messages(prompt_context)
    messages.append(
        {
            "role": "user",
            "content": json.dumps(
                {
                    "repair_instruction": "The previous answer was invalid. Return only one complete JSON object that matches required_json_schema.",
                    "previous_output": _compact_text(previous_output, 800),
                    "validation_error": _compact_text(validation_error, 300),
                    "required_json_schema": {
                        "primary_state": "unknown",
                        "secondary_flags": [],
                        "confidence": 0.0,
                        "assessment_status": "confirmed|uncertain|insufficient_evidence",
                        "self_regulation_detected": False,
                        "should_intervene_recommendation": False,
                        "evidence_sentences": [],
                        "reason": "",
                    },
                },
                ensure_ascii=False,
            ),
        }
    )
    return messages


class _HttpErrorStub(Exception):
    def __init__(self, code: int, msg: str):
        self.code = code
        super().__init__(msg)


def _request_openai(payload):
    gateway = get_gateway()
    result = gateway.call("state_detector", payload, response_type="json")
    if result.success:
        synthetic = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(result.output, ensure_ascii=False)
                        if isinstance(result.output, dict)
                        else str(result.output),
                    }
                }
            ]
        }
        if result.token_usage:
            synthetic["usage"] = result.token_usage
        return json.dumps(synthetic, ensure_ascii=False)

    if result.failure_type in (AUTHENTICATION_ERROR, UPSTREAM_5XX):
        status = 401 if result.failure_type == AUTHENTICATION_ERROR else 502
        raise _HttpErrorStub(status, result.failure_message or "LLM call failed")
    if result.failure_type in (NETWORK_ERROR, CONNECT_TIMEOUT, READ_TIMEOUT):
        raise TimeoutError(result.failure_message or "LLM timeout / network error")
    raise RuntimeError(result.failure_message or "LLM call failed")


def _normalize_result(payload, meta):
    if not isinstance(payload, dict):
        raise ValueError("payload_not_dict")

    payload = dict(payload)
    if "primary_state" not in payload and payload.get("state_code"):
        payload["primary_state"] = payload.get("state_code")
    if "should_intervene_recommendation" not in payload and "should_intervene" in payload:
        payload["should_intervene_recommendation"] = payload.get("should_intervene")

    required = {
        "primary_state",
        "secondary_flags",
        "confidence",
        "assessment_status",
        "self_regulation_detected",
        "should_intervene_recommendation",
        "evidence_sentences",
        "reason",
    }
    missing = sorted(required - set(payload.keys()))
    if missing:
        raise ValueError("missing_fields:" + ",".join(missing))

    primary_state = str(payload.get("primary_state") or "").strip()
    if primary_state not in ALLOWED_PRIMARY_STATES:
        raise ValueError("invalid_primary_state")

    assessment_status = str(payload.get("assessment_status") or "").strip()
    if assessment_status not in ALLOWED_ASSESSMENT_STATUS:
        raise ValueError("invalid_assessment_status")
    if assessment_status == "insufficient_evidence":
        primary_state = "unknown"

    confidence = _safe_float(payload.get("confidence"), -1.0)
    if confidence < 0.0 or confidence > 1.0:
        raise ValueError("invalid_confidence")

    secondary_flags = []
    if isinstance(payload.get("secondary_flags"), list):
        for item in payload["secondary_flags"][:8]:
            text = _compact_text(item, 80)
            if text:
                secondary_flags.append(text)
    else:
        raise ValueError("invalid_secondary_flags")

    evidence_sentences = []
    if isinstance(payload.get("evidence_sentences"), list):
        for item in payload["evidence_sentences"][:6]:
            text = _compact_text(item, 200)
            if text:
                evidence_sentences.append(text)
    else:
        raise ValueError("invalid_evidence_sentences")

    return {
        "primary_state": primary_state,
        "state_code": primary_state,
        "secondary_flags": secondary_flags,
        "confidence": round(confidence, 3),
        "assessment_status": assessment_status,
        "self_regulation_detected": bool(payload.get("self_regulation_detected")),
        "should_intervene_recommendation": bool(payload.get("should_intervene_recommendation")),
        "should_intervene": bool(payload.get("should_intervene_recommendation")),
        "evidence_sentences": evidence_sentences,
        "reason": _compact_text(payload.get("reason"), 600),
        "source": "llm",
        "model_name": meta["model_name"],
        "prompt_version": meta["prompt_version"],
        "latency_ms": meta["latency_ms"],
        "analysis_failed": False,
        "failure_reason": None,
        "validation_status": "passed",
        "schema_valid": True,
        "retry_count": int(meta.get("retry_count") or 0),
    }


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _base_meta():
    return {
        "model_name": SERA_LLM_MODEL,
        "prompt_version": LLM_ANALYZER_PROMPT_VERSION,
        "latency_ms": None,
        "analysis_failed": False,
        "failure_reason": None,
        "error_message": None,
        "analysis_skipped": False,
        "skip_reason": None,
        "request_started_at": None,
        "request_finished_at": None,
        "llm_required": True,
        "attempt_count": 0,
        "retry_count": 0,
        "max_attempts": max(1, int(PIPELINE_V2_LLM_MAX_JSON_RETRIES or 3)),
        "schema_valid": None,
        "validation_status": None,
        "validation_attempts": [],
    }


def run_group_llm_analysis(group_id, context, features=None, rule_result=None, request_impl=None):
    meta = _base_meta()
    student_messages = context.get("recent_student_messages") or []
    if not _llm_enabled():
        meta["analysis_skipped"] = True
        meta["skip_reason"] = "llm_disabled"
        return {"result": None, "meta": meta}
    if len(student_messages) < SEMANTIC_ANALYSIS_MIN_MESSAGES:
        meta["analysis_skipped"] = True
        meta["skip_reason"] = "insufficient_messages"
        return {"result": None, "meta": meta}

    prompt_context = build_llm_prompt_context(group_id, context, features=features, rule_result=rule_result)
    request_impl = request_impl or _request_openai
    meta["request_started_at"] = _now_str()
    overall_start = time.perf_counter()
    max_attempts = max(1, int(PIPELINE_V2_LLM_MAX_JSON_RETRIES or 3))
    last_raw_text = ""
    last_failure = None

    for attempt_index in range(max_attempts):
        meta["attempt_count"] = attempt_index + 1
        meta["retry_count"] = attempt_index
        payload = {
            "temperature": 0.1 if attempt_index == 0 else 0.0,
            "response_format": {"type": "json_object"},
            "messages": (
                _prompt_messages(prompt_context)
                if attempt_index == 0
                else _repair_prompt_messages(prompt_context, last_raw_text, last_failure or "")
            ),
        }
        try:
            raw_text = request_impl(payload)
            last_raw_text = str(raw_text or "")
            normalized = _normalize_result(_extract_json_payload(raw_text), meta)
            meta["latency_ms"] = int((time.perf_counter() - overall_start) * 1000)
            meta["request_finished_at"] = _now_str()
            meta["schema_valid"] = True
            meta["validation_status"] = "passed"
            return {"result": normalized, "meta": meta}
        except _HttpErrorStub as exc:
            last_failure = f"http_error_{exc.code}"
            meta["failure_reason"] = last_failure
            meta["error_message"] = str(exc)[:200]
            break
        except TimeoutError:
            last_failure = "timeout"
            meta["failure_reason"] = last_failure
            break
        except json.JSONDecodeError as exc:
            last_failure = "json_parse_failed:" + str(exc)[:160]
        except ValueError as exc:
            last_failure = str(exc)
        except Exception as exc:
            last_failure = exc.__class__.__name__
            meta["error_message"] = str(exc)[:200]
            logger.warning("run_group_llm_analysis unexpected error: %s", exc)
            break

        meta["validation_attempts"].append(
            {
                "attempt": attempt_index + 1,
                "error": _compact_text(last_failure, 240),
            }
        )

    meta["analysis_failed"] = True
    meta["failure_reason"] = meta.get("failure_reason") or last_failure or "json_validation_failed"
    meta["error_message"] = meta.get("error_message") or meta.get("failure_reason")
    meta["schema_valid"] = False
    meta["validation_status"] = "failed"
    meta["fallback_required"] = True
    if meta["latency_ms"] is None:
        meta["latency_ms"] = int((time.perf_counter() - overall_start) * 1000)
    if not meta.get("request_finished_at"):
        meta["request_finished_at"] = _now_str()
    return {"result": None, "meta": meta}


def analyze_group_llm(group_id, context, features=None, rule_result=None, request_impl=None):
    return run_group_llm_analysis(
        group_id,
        context,
        features=features,
        rule_result=rule_result,
        request_impl=request_impl,
    )["result"]


def generate_llm_intervention(group_id, context, strategy_info, condition, context_summary=""):
    """Generate a contextual intervention message via LLMGateway (intervention_generator profile).
    This is the "learning assistant" role, separate from state analysis.
    """
    if not _llm_enabled():
        return None

    anonymized = anonymize_context_for_llm(context)
    task = context.get("current_task") or {}
    sub_category = strategy_info.get("sub_category") or "general"
    strategy_type = strategy_info.get("strategy_type") or "general_support"
    template_guidance = strategy_info.get("message") or ""

    is_experiment = condition != "control"
    g1 = "可以进行过程性引导，如轮流表达、观点澄清、任务拆解、角色分工、目标重聚焦等"
    g2 = "只提供一般性情绪支持，不要给出明确步骤性建议"
    group_label = "实验组" if is_experiment else "对照组"
    guidance_style = g1 if is_experiment else g2

    system_prompt = (
        "你是嵌入式旁观型协同学习情绪-协作调节智能体 SERA。\n"
        "你的任务是根据当前小组讨论情况和策略指导，生成一段简短自然（2-3句）的提示，"
        "帮助小组改善协作过程。\n"
        "要求：\n"
        "- 不要替学生完成任务，不要给出任务答案\n"
        "- 语气自然、支持性、鼓励性，像一位有经验的助教\n"
        f"- {guidance_style}\n"
        "- 直接输出提示文本，不要加引号或前缀"
    )

    recent_messages = anonymized.get("recent_student_messages", [])[-8:]
    conversation_text = "\n".join(
        f"{m['speaker']}: {m['content']}" for m in recent_messages
    ) if recent_messages else context_summary[:500]

    user_prompt_text = (
        f"【当前任务】{task.get('title', '协作学习任务')}\n"
        f"【问题】{task.get('question', '')[:200]}\n"
        f"【小组类型】{group_label}\n"
        f"【检测到的状况】{sub_category}\n"
        f"【策略类型】{strategy_type}\n"
        f"【策略参考】{template_guidance[:300]}\n"
        f"\n【最近对话】\n{conversation_text}\n"
        f"\n请根据以上信息，生成一段简短自然的提示（2-3句），帮助当前小组推进讨论。"
    )

    payload = {
                "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt_text},
        ],
    }

    try:
        gateway = get_gateway()
        result = gateway.call("intervention_generator", payload, response_type="text")
        if result.success and result.output:
            cleaned = str(result.output).strip().strip("\"'\n ").strip()
            return cleaned if cleaned else None
        if result.failure_type:
            logger.warning(
                "generate_llm_intervention failed [%s]: %s",
                result.failure_type, result.failure_message,
            )
        return None
    except Exception as exc:
        logger.error("generate_llm_intervention unexpected error: %s", exc, exc_info=True)
        return None

def generate_student_help_response(group_id, context, strategy_meta, condition, context_rows, help_request_text):
    if not _llm_enabled():
        return None

    anonymized = anonymize_context_for_llm(context) if isinstance(context, dict) else {"recent_student_messages": []}
    task = context.get("current_task") or {}

    template_guidance = strategy_meta.get("template_guidance") or ""

    system_prompt = (
        "\u4f60\u662fSERA\uff0c\u8bfe\u5802\u5c0f\u7ec4\u8ba8\u8bba\u4e2d\u7684\u65c1\u89c2\u578b\u5b66\u4e60\u52a9\u624b\u3002\u5b66\u751f\u521a\u521a\u4e3b\u52a8\u6c42\u52a9\uff0c\u4f60\u7684\u56de\u590d\u4f1a\u76f4\u63a5\u53d1\u9001\u7ed9\u5c0f\u7ec4\u3002\n\n"
        "\u76ee\u6807\uff1a\n"
        "\u7528\u5f88\u77ed\u7684\u8bdd\u56de\u5e94\u6c42\u52a9\uff0c\u5e76\u5e2e\u52a9\u4ed6\u4eec\u7ee7\u7eed\u8ba8\u8bba\u3002\n\n"
        "\u786c\u6027\u89c4\u5219\uff1a\n"
        "1. \u53ea\u8f93\u51fa\u5b66\u751f\u53ef\u89c1\u6587\u672c\uff0c\u4e0d\u8981\u6807\u9898\u3001\u7f16\u53f7\u3001\u5f15\u53f7\u6216\u524d\u7f00\u3002\n"
        "2. \u56de\u590d\u4ee5 30 \u4e2a\u6c49\u5b57\u4ee5\u5185\u4e3a\u76ee\u6807\uff0c\u6700\u591a\u4e0d\u5f97\u8d85\u8fc7 40 \u4e2a\u6c49\u5b57\uff08\u6807\u70b9\u8ba1\u5165\u5b57\u6570\uff09\uff1b\u8d85\u51fa\u65f6\u5fc5\u987b\u4e3b\u52a8\u538b\u7f29\u63aa\u8f9e\uff0c\u6700\u591a2\u53e5\u3002\n"
        "3. \u5148\u77ed\u6682\u56de\u5e94\u6c42\u52a9\uff0c\u518d\u7ed91\u4e2a\u6700\u5c0f\u884c\u52a8\u3002\n"
        "4. \u4e0d\u8981\u66ff\u5b66\u751f\u5b8c\u6210\u4efb\u52a1\uff0c\u4e0d\u8981\u7ed9\u4efb\u52a1\u7b54\u6848\u3002\n"
        "5. \u4e0d\u8981\u957f\u7bc7\u5b89\u6170\uff0c\u4e0d\u8981\u89e3\u91ca\u7406\u8bba\uff0c\u4e0d\u8981\u590d\u8ff0\u4efb\u52a1\u6750\u6599\u3002\n"
        "6. \u4e0d\u8981\u6279\u8bc4\u5b66\u751f\uff0c\u4e0d\u8981\u8bf4\u201c\u4f60\u4eec\u5e94\u8be5/\u4f60\u4eec\u5fc5\u987b\u201d\u3002\n"
        "7. \u82e5\u4e3a\u5b9e\u9a8c\u7ec4\uff0c\u53ef\u4ee5\u7ed9\u4e00\u4e2a\u5177\u4f53\u8fc7\u7a0b\u6027\u5c0f\u6b65\u9aa4\u3002\n"
        "8. \u82e5\u4e3a\u5bf9\u7167\u7ec4\uff0c\u53ea\u7ed9\u4e00\u822c\u6027\u652f\u6301\uff0c\u4e0d\u7ed9\u660e\u786e\u6b65\u9aa4\u3002\n"
    )

    cl = []
    for r in (context_rows or [])[-6:]:
        if not isinstance(r, dict):
            r = dict(r)
        nm = r.get("real_name") or r.get("username") or "?"
        ct = r.get("content") or ""
        cl.append(str(nm) + ": " + str(ct))
    conversation_text = "\n".join(cl)

    user_prompt_text = (
        "\u3010\u5b66\u751f\u6c42\u52a9\u3011" + help_request_text + "\n"
        "\u3010\u5f53\u524d\u4efb\u52a1\u3011" + (task.get("title") or "\u534f\u4f5c\u5b66\u4e60\u4efb\u52a1") + "\n"
        "\u3010\u6210\u679c\u8981\u6c42\u3011" + ((task.get("output_requirement") or "")[:200]) + "\n"
        "\u3010\u5c0f\u7ec4\u7c7b\u578b\u3011" + ("experiment" if condition != "control" else "control") + "\n"
        "\u3010\u7b56\u7565\u53c2\u8003\u3011" + (template_guidance[:200]) + "\n"
        "\n\u3010\u6700\u8fd1\u5bf9\u8bdd\u3011\n" + conversation_text + "\n"
        "\n\u8bf7\u751f\u6210\u4e00\u6bb5\u77ed\u77ed\u7684\u56de\u5e94\u548c\u5f15\u5bfc:"
    )

    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt_text},
        ],
    }

    try:
        gateway = get_gateway()
        result = gateway.call("student_help", payload, response_type="text")
        if result.success and result.output:
            cleaned = str(result.output).strip().strip("\"'\n ").strip()
            return cleaned if cleaned else None
        return None
    except Exception as exc:
        logger.error("generate_student_help_response unexpected error: %s", exc, exc_info=True)
        return None
