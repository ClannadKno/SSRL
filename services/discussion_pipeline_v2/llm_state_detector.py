# -*- coding: utf-8 -*-
"""Strict multi-segment LLM state detector for incremental candidate windows."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

from config import (
    PIPELINE_V2_ANALYZER_VERSION,
    SERA_LLM_ENABLED,
    STATE_LLM_MAX_EVIDENCE_PER_SEGMENT,
    STATE_LLM_MAX_SEGMENTS,
    STATE_LLM_OUTPUT_MAX_TOKENS,
    STATE_LLM_REPAIR_OUTPUT_MAX_TOKENS,
    STATE_LLM_SCHEMA_MAX_ATTEMPTS,
)
from db import query_one
from services.audit_log_service import safe_write_audit_log
from services.llm_gateway import get_gateway
from services.state_strategy_router import normalize_sub_category
from services.three_stage_schema import (
    CANONICAL_SUB_STATE_SEMANTICS,
    OI_STRATEGY_IDS,
    OVERLAY_COMPATIBLE_PRIMARY_STATES,
    STAGE2_SCHEMA_VERSION,
    STAGE2_STATE_BOUNDARY_GUIDANCE,
    STAGE2_MODEL_OUTPUT_STATE_CODES,
    STATE_OVERLAY_CODES,
    STATE_OVERLAY_SEMANTICS,
    legacy_state_for_sub_state,
    route_for_state_with_overlays,
)
from services.three_stage_latency import (
    elapsed_ms,
    latency_timer,
    normalize_stage2_failure,
    record_latency_event,
)


logger = logging.getLogger(__name__)


SEGMENT_STATE_CODES = {
    "positive_collaboration",
    "conflict_tension",
    "negative_silence",
    "frustration_stuck",
    "task_detached",
}
RISK_SEGMENT_STATE_CODES = SEGMENT_STATE_CODES - {"positive_collaboration"}
INTERVENTION_REASON_CODES = {
    "active_conflict",
    "group_stuck",
    "clear_task_detachment",
    "prolonged_negative_silence",
    "explicit_help_request",
    "risk_already_recovered",
    "constructive_progress",
    "insufficient_evidence",
    "continue_observing",
}

# Compatibility projection used by the existing rule-fusion/audit path.  New
# collaboration_state_segments always retain the Batch 3 public state name.
_LEGACY_STATE_CODES = {"frustration_stuck": "blocked_frustration"}

STATE_DETECTOR_RETRY_TEMPERATURE = 0.01
MAX_SECONDARY_TAGS = 4
STATE_ONLY_OUTPUT_CODES = tuple(
    dict.fromkeys((*STAGE2_MODEL_OUTPUT_STATE_CODES, *STATE_OVERLAY_CODES))
)
SUB_CATEGORY_CANONICAL_COMPATIBILITY = {
    code: [code] for code in STAGE2_MODEL_OUTPUT_STATE_CODES
}
SUB_CATEGORY_CANONICAL_COMPATIBILITY.update(
    {
        overlay: list(OVERLAY_COMPATIBLE_PRIMARY_STATES.get(overlay, ()))
        for overlay in STATE_OVERLAY_CODES
    }
)
STATE_ONLY_FORBIDDEN_FIELDS = {
    "should_intervene",
    "inhibition",
    "candidate_strategy_ids",
    "strategy_id",
    "selected_strategy_id",
    "strategy_pool",
    "intervention",
    "intervention_message",
    "message",
}


_STATE_DETECTOR_SYSTEM_PROMPT = """你是 SSRL-ESP 协同学习状态识别器。

你的唯一任务是根据固定消息窗口、任务背景和第一阶段规则证据，识别精确的协作子状态。

你负责判断“当前发生了什么”，不负责生成面向学生的介入话术。

必须遵守：
1. 只能使用输入中提供的 candidate_messages 学生消息作为状态证据。
2. context_only、Agent 消息、系统消息和教师消息只能作为背景，不能作为学生状态的核心证据。
3. 只输出窗口末尾仍活动的当前精确状态；窗口中的过渡状态只能作为判断背景，不输出多个段。
4. evidence_message_ids 必须逐项来自 evidence_contract.allowed_candidate_sequences；不得使用 context_only_sequences，不得猜测、替换或虚构编号。
5. sub_category 必须来自 allowed_sub_categories，canonical_state 必须来自 allowed_canonical_sub_states，并严格满足 sub_category_to_compatible_canonical_states 映射。
6. canonical_sub_state_semantics 是状态语义的权威定义，必须逐项应用 definition 和 boundary，不能只根据英文 code 猜测。
7. 判断深度思考时必须联合检查：思考前的独立核算/查阅/比较声明、消息 timestamp 显示的约30到120秒任务对齐低互动、以及思考后的实质推理结果；context_only 可证明思考前背景，但核心结果证据仍必须来自 candidate_messages。
8. 必须区分建设性冲突与人际性冲突、深度思考与执行推进/普通讨论、困惑/挫败/倦怠、倦怠与敷衍脱离、跑题后已自主拉回与尚未自主拉回、执行推进与消极低参与。
9. 若同一成员的任务相关建议在 context_only 与 candidate_messages 中重复出现，且 candidate_messages 末尾明确搁置或绕过该成员并继续推进，应优先判为 individual_marginalization 而非 execution_progress；candidate_messages 中的再次提议和搁置语句可以作为核心证据。
10. individual_marginalization 不得仅由冲突参与者一句“算了、按你们的来”推断；当 rule_hints 的 conflict_tension 很高且 candidate_messages 仍是相互攻击、指责或防御性对抗时，应优先判为 interpersonal_conflict。只有同一成员此前多次提出中性任务建议、问题或参与请求并被他人反复绕过，才判 individual_marginalization。
11. individual_marginalization 必须存在稳定针对同一成员的排除关系；若多名成员都在轮流说“随便、差不多、能交就行”并共同给出空泛最低限度内容，应判为 perfunctory_detachment。某一成员偶尔提醒交付要求但仍被任务性回应，不足以构成个体边缘化。
12. 发现小组正在自发调节时，应优先保护自发调节，只识别状态，不做介入决策。
13. OI 类状态只需要识别为对应 sub_category 或 canonical_state，不得输出 OI 策略编号。
14. 不得判断 should_intervene，不得输出 candidate_strategy_ids、strategy_id、strategy_pool 或任何话术。
15. 不得输出学生可见话术、建议话术、Markdown 或额外解释。
16. 不确定时输出 unknown_sub_state，不得猜测。
17. 必须严格输出 JSON。

顶层必须且只能按以下契约组织（字段名不得改写）：
{
  "sub_category": "allowed_sub_categories 中的一个值",
  "canonical_state": "allowed_canonical_sub_states 中的一个值；若 sub_category 是 stage_achievement，可填写 execution_progress",
  "confidence": 0.0到1.0,
  "evidence_message_ids": [candidate_messages 中的 sequence]
}

sub_category 是本阶段唯一需要识别的精确状态；canonical_state 是其规范化状态。
burnout 必须独立输出为 burnout，禁止归并为 frustration。"""


_STATE_DETECTOR_SYSTEM_PROMPT += """
18. 先按 state_boundary_guidance 判断 primary state，再判断 secondary overlay；这些规则是判别说明，不是当前窗口证据。
19. 持续尝试并提供具体任务内容属于 frustration，不是 perfunctory_detachment；重复失败和泄气才支持 frustration，低投入、空泛“随便”才支持 perfunctory_detachment。
20. 只围绕方案、证据和共同标准争论且仍能整合时属于 constructive_conflict；针对成员的攻击、归责、讽刺或防御性对抗才属于 interpersonal_conflict。
21. high_intensity_overload 只能作为 secondary overlay；仅有信息密集、任务复杂或表达强烈不得改判 interpersonal_conflict。若输出该 overlay，canonical_state 必须明确填写兼容的 primary state，不能填写 overlay 自身。
22. 障碍、冲突或过载在窗口末尾已经恢复时，只输出恢复后的当前状态；不得把已恢复的过渡状态继续作为 active state。
"""

_STATE_DETECTOR_SYSTEM_PROMPT += """
23. secondary_tags are optional overlays, not primary states. Evaluate every allowed_secondary_tag against secondary_tag_semantics.
24. stage_achievement is a routable sub_category: use sub_category="stage_achievement" only with explicit completed-milestone evidence, and set canonical_state="execution_progress".
25. Explicit fear of speaking after ridicule or personal attack is psychological_safety_risk; simultaneous information/constraint overload is high_intensity_overload; an explicitly completed milestone is stage_achievement.
26. burnout is not frustration: value-questioning, meaning loss, "没意思/做了也白做/不想继续" must output burnout.
"""

_RETRY_STATE_DETECTOR_SYSTEM_PROMPT = """修复上一轮回答，只返回符合状态识别合同的紧凑 JSON。
不得解释，不得输出 Markdown，不得生成学生可见话术。只能使用 candidate_messages 的 sequence 编号作为边界和证据。
必须逐项对照 evidence_contract.allowed_candidate_sequences；context_only_sequences 和其他编号都不得作为 evidence。不得猜测、替换或虚构编号。
必须逐项对照 sub_category_to_compatible_canonical_states；primary sub_category 只能映射到自身，overlay 只能映射到列出的 compatible canonical_state。
必须重新应用 canonical_sub_state_semantics：高 conflict_tension 且仍在相互攻击或指责时优先 interpersonal_conflict；冲突参与者一句“算了、按你们的来”本身不构成 individual_marginalization。
多人共同给出“随便、差不多、能交就行”等最低限度内容且不存在稳定的定向排除关系时，必须判 perfunctory_detachment，而不是 individual_marginalization。
持续尝试并提供实质任务内容时判 frustration，不要因为语气泄气而判 perfunctory_detachment；只因信息密集或负荷高不能判 interpersonal_conflict。high_intensity_overload 是 overlay，canonical_state 必须填写明确的兼容 primary state，不得填写 overlay 自身。
只允许返回 sub_category、canonical_state、confidence、evidence_message_ids；不得返回 should_intervene、candidate_strategy_ids、strategy_id 或话术。"""


def _stage2_prompt_metrics(payload: dict) -> dict:
    """Return prompt shape metrics without retaining prompt contents."""

    messages = payload.get("messages") if isinstance(payload, dict) else []
    prompt = "\n".join(
        str(message.get("content") or "")
        for message in (messages or [])
        if isinstance(message, dict)
    )
    character_count = len(prompt)
    estimated_tokens = max(1, (character_count + 3) // 4) if character_count else 0
    return {
        "prompt_character_count": character_count,
        "prompt_estimated_tokens": estimated_tokens,
        # Keep the Stage 3 naming available to shared telemetry consumers.
        "prompt_chars": character_count,
    }


def _stage2_model_content(result, raw_output: Any) -> Any:
    """Extract provider content for diagnostics, never provider reasoning."""

    raw_text = getattr(result, "raw_text", None)
    if isinstance(raw_text, str) and raw_text.strip():
        try:
            envelope = json.loads(raw_text)
        except (TypeError, ValueError):
            envelope = None
        if isinstance(envelope, dict) and envelope.get("choices"):
            message = (envelope.get("choices") or [{}])[0].get("message") or {}
            if isinstance(message, dict) and "content" in message:
                content = message.get("content")
                if isinstance(content, list):
                    return "".join(
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict)
                    )
                return content
    return raw_output


def _stage2_usage_metrics(result, model_content: Any) -> dict:
    """Expose bounded usage/content-channel signals for one Stage 2 call."""

    usage = getattr(result, "token_usage", None)
    usage = usage if isinstance(usage, dict) else {}
    completion_details = usage.get("completion_tokens_details")
    completion_details = (
        completion_details if isinstance(completion_details, dict) else {}
    )

    def _non_negative_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    completion_tokens = _non_negative_int(usage.get("completion_tokens"))
    reasoning_tokens = _non_negative_int(
        usage.get("reasoning_tokens")
        if usage.get("reasoning_tokens") is not None
        else completion_details.get("reasoning_tokens")
    )
    if isinstance(model_content, str):
        final_content_empty = not bool(model_content.strip())
    else:
        final_content_empty = model_content is None

    reasoning_content_present = False
    raw_text = getattr(result, "raw_text", None)
    if isinstance(raw_text, str) and raw_text.strip():
        try:
            envelope = json.loads(raw_text)
        except (TypeError, ValueError):
            envelope = None
        if isinstance(envelope, dict) and envelope.get("choices"):
            message = (envelope.get("choices") or [{}])[0].get("message") or {}
            reasoning_content_present = bool(
                isinstance(message, dict)
                and str(message.get("reasoning_content") or "").strip()
            )

    reasoning_budget_exhausted = bool(
        final_content_empty
        and completion_tokens > 0
        and reasoning_tokens > 0
        and reasoning_tokens == completion_tokens
    )
    if getattr(result, "failure_type", None) == "reasoning_budget_exhausted":
        reasoning_budget_exhausted = True
    return {
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "reasoning_content_present": reasoning_content_present,
        "final_content_empty": final_content_empty,
        "reasoning_budget_exhausted": reasoning_budget_exhausted,
    }


_STAGE2_LOCALLY_PARSEABLE_FAILURES = frozenset(
    {
        "truncated_response",
        "invalid_response",
        "json_parse_error",
    }
)


def _stage2_should_locally_parse(result, model_content: Any) -> bool:
    """Allow local parsing of content even when the gateway flagged truncation."""

    if result is None or model_content in (None, ""):
        return False
    if bool(getattr(result, "success", False)):
        return True
    if getattr(result, "output", None) is not None:
        return True
    return getattr(result, "failure_type", None) in _STAGE2_LOCALLY_PARSEABLE_FAILURES


def _json_object_candidates(content: Any) -> tuple[list[dict], bool]:
    """Extract complete JSON objects while tolerating surrounding prose.

    ``json.loads`` requires the complete response to be JSON.  Providers can
    still return a usable object wrapped in a Markdown fence or a short
    explanation, so use ``raw_decode`` at each object boundary instead.  The
    second return value is only a structural signal for an unterminated JSON
    container; it is never used to fabricate a result.
    """

    if isinstance(content, dict):
        return [content], False
    if not isinstance(content, str):
        return [], False
    text = content.strip().lstrip("\ufeff")
    if not text:
        return [], False

    decoder = json.JSONDecoder()
    candidates: list[dict] = []
    seen_starts: set[int] = set()
    for index, character in enumerate(text):
        if character != "{" or index in seen_starts:
            continue
        seen_starts.add(index)
        try:
            value, _end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(value)

    stack: list[str] = []
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            stack.append(character)
        elif character in "]}":
            expected = "[" if character == "]" else "{"
            if stack and stack[-1] == expected:
                stack.pop()
            elif stack:
                stack.clear()
                break

    return candidates, bool(stack)


def _diagnostic_json_value(content_text: str) -> Any:
    text = str(content_text or "").strip()
    if not text:
        return None
    candidates, _incomplete = _json_object_candidates(text)
    if candidates:
        return candidates[0]
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character not in "[":
            continue
        try:
            value, _end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            return value
    return None


def _stage2_attempt_diagnostics(
    *,
    payload: dict,
    gateway,
    result,
    raw_output: Any,
    parser_result: Optional[dict] = None,
    failure_reason: Any = None,
    error_message: Any = None,
    exception: bool = False,
) -> dict:
    """Build bounded evidence for one Stage 2 external call."""

    profile = getattr(gateway, "profiles", {}).get(LLMStateDetector.STATE_DETECTOR_PROFILE)
    model_content = _stage2_model_content(result, raw_output)
    if isinstance(model_content, str):
        content_text = model_content.strip()
    elif model_content is None:
        content_text = ""
    else:
        try:
            content_text = json.dumps(
                model_content,
                ensure_ascii=False,
                separators=(",", ":"),
            ).strip()
        except (TypeError, ValueError):
            content_text = str(model_content).strip()

    json_value = _diagnostic_json_value(content_text)
    json_extractable = isinstance(json_value, (dict, list))
    open_brace_present = "{" in content_text
    close_brace_present = "}" in content_text
    open_bracket_present = "[" in content_text
    close_bracket_present = "]" in content_text
    finish_reason = getattr(result, "finish_reason", None)
    gateway_failure_type = getattr(result, "failure_type", None)
    usage_metrics = _stage2_usage_metrics(result, model_content)
    response_incomplete = bool(
        gateway_failure_type == "truncated_response"
        or usage_metrics["reasoning_budget_exhausted"]
        or str(finish_reason or "").strip().lower() == "length"
        or (open_brace_present and not close_brace_present)
        or (open_bracket_present and not close_bracket_present)
    )
    parser_status = None
    if parser_result is not None:
        parser_status = (
            "valid"
            if parser_result.get("valid")
            else parser_result.get("error_type") or "schema_validation_error"
        )
    else:
        parser_status = failure_reason or gateway_failure_type
    resolved_reason = failure_reason or gateway_failure_type
    resolved_error = error_message or getattr(result, "failure_message", None)
    failure_category = None
    if parser_result is None or not parser_result.get("valid"):
        failure_category = normalize_stage2_failure(
            resolved_reason,
            finish_reason=finish_reason,
            attempt_type=None,
            error_message=(
                (parser_result or {}).get("error_message")
                or resolved_error
            ),
            response_incomplete=response_incomplete,
            exception=exception,
        )
    attempt_count = getattr(result, "attempt_count", None)
    try:
        gateway_attempt_count = max(1, int(attempt_count))
    except (TypeError, ValueError):
        gateway_attempt_count = 0
    explicit_gateway_retries = getattr(result, "gateway_retry_count", None)
    try:
        resolved_gateway_retry_count = max(0, int(explicit_gateway_retries))
    except (TypeError, ValueError):
        resolved_gateway_retry_count = max(0, gateway_attempt_count - 1)
    try:
        compatibility_fallback_count = max(
            0,
            int(getattr(result, "compatibility_fallback_count", 0) or 0),
        )
    except (TypeError, ValueError):
        compatibility_fallback_count = 0
    prompt_metrics = _stage2_prompt_metrics(payload)
    evidence_diagnostics = _evidence_validation_diagnostics(parser_result)
    return {
        **prompt_metrics,
        **usage_metrics,
        **evidence_diagnostics,
        "model": getattr(result, "model_name", None) or getattr(profile, "model", None),
        "profile": getattr(result, "profile_name", None)
        or LLMStateDetector.STATE_DETECTOR_PROFILE,
        "max_tokens": payload.get("max_tokens"),
        "timeout": getattr(profile, "read_timeout", None),
        "timeout_seconds": getattr(profile, "read_timeout", None),
        "gateway_retries": getattr(profile, "retries", None),
        "gateway_attempt_count": gateway_attempt_count,
        "gateway_retry_count": resolved_gateway_retry_count,
        "compatibility_fallback_count": compatibility_fallback_count,
        "external_call_count": gateway_attempt_count,
        "final_content_only": bool(payload.get("_sera_final_content_only")),
        "thinking_control": (
            (payload.get("thinking") or {}).get("type")
            if isinstance(payload.get("thinking"), dict)
            else None
        ),
        "response_character_count": len(content_text),
        "response_chars": len(content_text),
        "finish_reason": finish_reason,
        "parser_result": parser_status,
        "json_extractable": json_extractable,
        "open_brace_present": open_brace_present,
        "close_brace_present": close_brace_present,
        "response_starts_with_brace": content_text.startswith("{"),
        "response_ends_with_brace": content_text.endswith("}"),
        "response_incomplete": response_incomplete,
        "incomplete_response": response_incomplete,
        "local_parse_success": bool(parser_result and parser_result.get("valid")),
        "error": str(resolved_error or "")[:240] or None,
        "failure_category": failure_category,
    }


def _safe_response_summary(diagnostics: dict) -> str:
    """Serialize response shape only; never persist response text."""

    fields = (
        "response_character_count",
        "finish_reason",
        "parser_result",
        "json_extractable",
        "open_brace_present",
        "close_brace_present",
        "response_incomplete",
        "failure_category",
        "reasoning_tokens",
        "completion_tokens",
        "reasoning_content_present",
        "final_content_empty",
        "reasoning_budget_exhausted",
        "compatibility_fallback_count",
        "evidence_valid_sequence_count",
        "evidence_rejected_sequence_count",
        "evidence_duplicate_sequence_count",
        "evidence_input_order_normalized",
        "evidence_filtered",
        "evidence_sequence_inference_used",
    )
    return json.dumps(
        {field: diagnostics.get(field) for field in fields},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _resolve_stage2_pipeline_run_id(context: dict) -> Optional[int]:
    value = (context or {}).get("pipeline_run_id")
    try:
        if value is not None:
            return int(value)
    except (TypeError, ValueError):
        pass
    batch_id = (context or {}).get("assessment_batch_id")
    try:
        batch_id = int(batch_id) if batch_id is not None else None
    except (TypeError, ValueError):
        batch_id = None
    if batch_id is None:
        return None
    try:
        row = query_one(
            """
            SELECT id
            FROM strategy_pipeline_runs
            WHERE assessment_batch_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (batch_id,),
        )
        return int(row["id"]) if row else None
    except Exception:
        return None


def _write_llm_audit(
    context: dict,
    result,
    failure: str = None,
    diagnostics: dict = None,
) -> None:
    """Write bounded call metadata without prompts or student conversation text."""
    diagnostics = dict(diagnostics or {})
    metadata = {
        "group_id": (context or {}).get("group_id"),
        "session_id": (context or {}).get("session_id"),
        "discussion_id": (context or {}).get("discussion_id"),
        "pipeline_run_id": (context or {}).get("pipeline_run_id"),
        "assessment_batch_id": (context or {}).get("assessment_batch_id"),
        "batch_scheduler_attempt_count": (context or {}).get(
            "batch_scheduler_attempt_count"
        ),
        "batch_scheduler_max_attempts": (context or {}).get(
            "batch_scheduler_max_attempts"
        ),
        "candidate_start_sequence": (context or {}).get("candidate_start_sequence"),
        "candidate_end_sequence": (context or {}).get("candidate_end_sequence"),
        "detector": "LLMStateDetector",
        "profile": LLMStateDetector.STATE_DETECTOR_PROFILE,
        "success": bool(
            not failure
            and (
                diagnostics.get("parser_result") == "valid"
                or diagnostics.get("accepted_after_local_parse")
                or (
                    diagnostics.get("parser_result") is None
                    and result
                    and result.success
                )
            )
        ),
        "latency_ms": getattr(result, "latency_ms", None),
        "model": getattr(result, "model_name", None),
        "finish_reason": getattr(result, "finish_reason", None),
        "raw_text_length": len(getattr(result, "raw_text", None) or ""),
        "failure_type": getattr(result, "failure_type", None) or failure,
    }
    metadata.update(
        {
            key: value
            for key, value in diagnostics.items()
            if key
            in {
                "attempt_type",
                "model",
                "profile",
                "max_tokens",
                "timeout",
                "gateway_retry_count",
                "prompt_character_count",
                "prompt_estimated_tokens",
                "response_character_count",
                "finish_reason",
                "parser_result",
                "json_extractable",
                "open_brace_present",
                "close_brace_present",
                "response_incomplete",
                "failure_category",
                "accepted_after_local_parse",
                "reasoning_tokens",
                "completion_tokens",
                "reasoning_content_present",
                "final_content_empty",
                "reasoning_budget_exhausted",
                "compatibility_fallback_count",
                "final_content_only",
                "thinking_control",
                "evidence_validation_policy",
                "evidence_valid_sequence_count",
                "evidence_rejected_sequence_count",
                "evidence_rejected_sequences",
                "evidence_rejected_reason_counts",
                "evidence_duplicate_sequence_count",
                "evidence_input_order_normalized",
                "evidence_filtered",
                "evidence_sequence_inference_used",
            }
        }
    )
    safe_write_audit_log(
        action_type="llm.call",
        actor_type="system",
        actor_id="pipeline_v2",
        target_type="group",
        target_id=(context or {}).get("group_id"),
        metadata=metadata,
    )


def _as_sequence(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bounded_text(value: Any, limit: int) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _explicit_overlay_tags(canonical: str, text: str) -> list[str]:
    value = str(text or "")
    tags = []
    if canonical == "interpersonal_conflict":
        direct = (
            "不敢表达", "不敢发言", "不敢参与", "不敢说", "被笑",
            "嘲笑", "羞辱", "人身攻击", "心理安全", "安全风险", "被针对",
        )
        if any(phrase in value for phrase in direct):
            tags.append("psychological_safety_risk")
    if canonical in OVERLAY_COMPATIBLE_PRIMARY_STATES["high_intensity_overload"]:
        direct = (
            "信息太多", "反应不过来", "注意力跟不上", "所有限制一起",
            "持续过载", "高强度过载", "同时盯着", "完全理不清",
        )
        if any(phrase in value for phrase in direct):
            tags.append("high_intensity_overload")
    if canonical == "execution_progress":
        direct = (
            "阶段目标完成", "这一阶段完成", "已经通过检查", "已通过检查",
            "里程碑完成", "交付物完成", "全部完成", "已经补齐", "已经写完",
        )
        if any(phrase in value for phrase in direct):
            tags.append("stage_achievement")
    return list(dict.fromkeys(tags))


def _enrich_explicit_overlay_tags(data: dict, candidate_messages: list[dict]) -> dict:
    messages = {
        _as_sequence(item.get("sequence")): str(item.get("content") or "")
        for item in candidate_messages or []
        if isinstance(item, dict) and _as_sequence(item.get("sequence")) is not None
    }
    segments = list(data.get("segments") or [])
    for segment in segments:
        start = _as_sequence(segment.get("start_sequence"))
        end = _as_sequence(segment.get("end_sequence"))
        if start is None or end is None:
            continue
        text = " ".join(
            messages[sequence]
            for sequence in sorted(messages)
            if start <= sequence <= end
        )
        inferred = _explicit_overlay_tags(
            str(segment.get("canonical_sub_state") or ""),
            text,
        )
        segment["secondary_tags"] = list(dict.fromkeys([
            *(segment.get("secondary_tags") or []),
            *inferred,
        ]))[:MAX_SECONDARY_TAGS]
    active_index = _as_sequence(data.get("active_segment_index"))
    if active_index is not None and 0 <= active_index < len(segments):
        data["active_sub_state"]["secondary_tags"] = list(
            segments[active_index].get("secondary_tags") or []
        )
    data["segments"] = segments
    return data


def _speaker_alias(index: int) -> str:
    return "S" + str(index + 1)


def _split_detector_messages(context: dict) -> dict:
    rows = (
        (context or {}).get("state_detector_messages")
        or (context or {}).get("recent_student_messages")
        or (context or {}).get("window_student_messages")
        or []
    )
    has_explicit_candidates = "state_detector_candidate_sequences" in (context or {})
    explicit_candidates = {
        value
        for value in (
            _as_sequence(item)
            for item in (context or {}).get("state_detector_candidate_sequences", [])
        )
        if value is not None
    }
    has_new_flags = any("is_new_since_last_assessment" in row for row in rows if isinstance(row, dict))
    alias_map = {}
    candidate_messages = []
    context_only = []
    sequence_to_message_id = {}

    for row in rows:
        if not isinstance(row, dict) or (row.get("role") or "student") == "agent":
            continue
        content = _bounded_text(row.get("content"), 400)
        sequence = _as_sequence(row.get("sequence"))
        # Legacy direct callers sometimes supplied only message ids.  The
        # production monitoring path always supplies explicit sequences.
        if sequence is None:
            sequence = _as_sequence(
                row.get("message_id") if row.get("message_id") is not None else row.get("id")
            )
        if sequence is None or not content:
            continue
        message_id = _as_sequence(
            row.get("message_id") if row.get("message_id") is not None else row.get("id")
        )
        if message_id is not None:
            sequence_to_message_id[sequence] = message_id
        speaker_key = (
            row.get("user_id")
            or row.get("participant_code")
            or row.get("username")
            or "unknown"
        )
        if speaker_key not in alias_map:
            alias_map[speaker_key] = _speaker_alias(len(alias_map))
        item = {
            "sequence": sequence,
            "speaker": alias_map[speaker_key],
            "timestamp": row.get("created_at"),
            "content": content,
        }
        if has_explicit_candidates:
            is_candidate = sequence in explicit_candidates
        elif has_new_flags:
            is_candidate = bool(row.get("is_new_since_last_assessment"))
        else:
            is_candidate = True
        (candidate_messages if is_candidate else context_only).append(item)

    candidate_messages.sort(key=lambda item: item["sequence"])
    context_only.sort(key=lambda item: item["sequence"])
    return {
        "candidate_messages": candidate_messages,
        "context_only": context_only,
        "candidate_sequences": [item["sequence"] for item in candidate_messages],
        "context_sequences": [item["sequence"] for item in context_only],
        "sequence_to_message_id": sequence_to_message_id,
    }


def _json_result(
    error_type: str = None,
    error_message: str = None,
    data: dict = None,
    evidence_validation: dict = None,
) -> dict:
    result = {
        "valid": data is not None,
        "data": data,
        "error_type": error_type,
        "error_message": error_message,
    }
    if evidence_validation is not None:
        result["evidence_validation"] = evidence_validation
    return result


def _schema_error(
    code: str,
    detail: str = None,
    *,
    evidence_validation: dict = None,
) -> dict:
    message = f"{code}:{detail}" if detail else code
    return _json_result(
        code,
        message,
        evidence_validation=evidence_validation,
    )


def _required_int(value: Any, code: str) -> int:
    if isinstance(value, bool):
        raise ValueError(code)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(code) from exc


def _compact_valid_evidence(evidence: list[int], max_items: int) -> list[int]:
    """Keep representative chronological evidence without inventing IDs.

    The model occasionally cites every message in a small valid segment.  The
    configured evidence cap is an audit-size limit, not a semantic reason to
    quarantine an otherwise valid assessment.  Keep the first, evenly spaced
    interior, and final evidence IDs so boundaries remain explainable.
    """

    if len(evidence) <= max_items:
        return list(evidence)
    if max_items <= 1:
        return [evidence[-1]]
    last_index = len(evidence) - 1
    selected_indexes = [
        (slot * last_index) // (max_items - 1)
        for slot in range(max_items)
    ]
    return [evidence[index] for index in selected_indexes]


class _EvidenceValidationError(ValueError):
    def __init__(self, code: str, validation: dict):
        super().__init__(code)
        self.validation = validation


def _normalize_candidate_evidence(
    raw_evidence: Any,
    *,
    candidate_set: set[int],
    candidate_position: dict[int, int],
    context_only_set: set[int] = None,
    segment_start: Optional[int] = None,
    segment_end: Optional[int] = None,
) -> tuple[list[int], list[int], dict]:
    """Validate evidence against the exact dynamic candidate set.

    Mixed output is repaired locally only by discarding cited sequences that
    are provably outside the candidate contract. No sequence is inferred,
    substituted, or synthesized.
    """

    if not isinstance(raw_evidence, list):
        raise ValueError("invalid_evidence_message_ids")
    context_only_set = set(context_only_set or ())
    valid_in_input_order = []
    rejected = []
    seen = set()
    duplicate_count = 0

    for raw_sequence in raw_evidence:
        sequence = _required_int(raw_sequence, "invalid_evidence_message_id")
        if sequence in seen:
            duplicate_count += 1
            continue
        seen.add(sequence)
        if sequence not in candidate_set:
            rejected.append(
                {
                    "sequence": sequence,
                    "reason": (
                        "context_only_not_evidence"
                        if sequence in context_only_set
                        else "outside_candidate"
                    ),
                }
            )
            continue
        if (
            segment_start is not None
            and segment_end is not None
            and not segment_start <= sequence <= segment_end
        ):
            rejected.append(
                {
                    "sequence": sequence,
                    "reason": "outside_segment",
                }
            )
            continue
        valid_in_input_order.append(sequence)

    valid = sorted(valid_in_input_order, key=candidate_position.__getitem__)
    compacted = _compact_valid_evidence(
        valid,
        int(STATE_LLM_MAX_EVIDENCE_PER_SEGMENT),
    )
    reason_counts = {}
    for item in rejected:
        reason = item["reason"]
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    validation = {
        "policy": "retain_valid_candidate_sequences_and_audit_rejections",
        "accepted_sequences": list(compacted),
        "valid_sequence_count": len(valid),
        "rejected_sequence_count": len(rejected),
        "rejected_sequences": rejected[:12],
        "rejected_reason_counts": reason_counts,
        "duplicate_sequence_count": duplicate_count,
        "input_order_normalized": valid_in_input_order != valid,
        "filtered": bool(rejected),
        "sequence_inference_used": False,
    }
    if not valid:
        error_code = (
            "evidence_outside_candidate"
            if any(
                item["reason"] in {"context_only_not_evidence", "outside_candidate"}
                for item in rejected
            )
            else "evidence_outside_segment"
        )
        if not rejected:
            error_code = "missing_evidence_message_ids"
        raise _EvidenceValidationError(error_code, validation)
    return compacted, valid, validation


def _evidence_validation_summary(validations: list[dict]) -> dict:
    validations = [dict(item) for item in validations if isinstance(item, dict)]
    return {
        "policy": "retain_valid_candidate_sequences_and_audit_rejections",
        "segments": validations,
        "valid_sequence_count": sum(
            int(item.get("valid_sequence_count") or 0) for item in validations
        ),
        "rejected_sequence_count": sum(
            int(item.get("rejected_sequence_count") or 0) for item in validations
        ),
        "duplicate_sequence_count": sum(
            int(item.get("duplicate_sequence_count") or 0) for item in validations
        ),
        "input_order_normalized": any(
            bool(item.get("input_order_normalized")) for item in validations
        ),
        "filtered": any(bool(item.get("filtered")) for item in validations),
        "sequence_inference_used": False,
    }


def _evidence_validation_diagnostics(parser_result: Optional[dict]) -> dict:
    if not isinstance(parser_result, dict):
        return {}
    if parser_result.get("valid"):
        source = (parser_result.get("data") or {}).get("evidence_validation")
    else:
        source = parser_result.get("evidence_validation")
    if not isinstance(source, dict):
        return {}
    rejected_sequences = []
    rejected_reason_counts = {}
    for segment in source.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        for item in segment.get("rejected_sequences") or []:
            if isinstance(item, dict) and len(rejected_sequences) < 12:
                rejected_sequences.append(
                    {
                        "sequence": item.get("sequence"),
                        "reason": item.get("reason"),
                    }
                )
        for reason, count in (
            segment.get("rejected_reason_counts") or {}
        ).items():
            rejected_reason_counts[str(reason)] = (
                rejected_reason_counts.get(str(reason), 0) + int(count or 0)
            )
    return {
        "evidence_validation_policy": source.get("policy"),
        "evidence_valid_sequence_count": int(
            source.get("valid_sequence_count") or 0
        ),
        "evidence_rejected_sequence_count": int(
            source.get("rejected_sequence_count") or 0
        ),
        "evidence_rejected_sequences": rejected_sequences,
        "evidence_rejected_reason_counts": rejected_reason_counts,
        "evidence_duplicate_sequence_count": int(
            source.get("duplicate_sequence_count") or 0
        ),
        "evidence_input_order_normalized": bool(
            source.get("input_order_normalized")
        ),
        "evidence_filtered": bool(source.get("filtered")),
        "evidence_sequence_inference_used": False,
    }


def _dedupe_strings(values: Any, *, allowed: set[str] = None, max_items: int = None) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("invalid_string_list")
    result = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        if allowed is not None and text not in allowed:
            raise ValueError(text)
        if text not in result:
            result.append(text)
        if max_items is not None and len(result) > max_items:
            raise ValueError("too_many_items")
    return result


def _canonical_from_payload(payload: dict, *, field: str = "canonical_sub_state") -> str:
    value = str(
        (payload or {}).get(field)
        or (payload or {}).get(f"{field}_code")
        or ""
    ).strip()
    if value not in STAGE2_MODEL_OUTPUT_STATE_CODES:
        raise ValueError("invalid_canonical_sub_state")
    return value


def _normalize_state_only_code(
    value: Any,
    error_code: str,
    *,
    allow_overlays: bool = True,
) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(error_code)
    normalized = normalize_sub_category(raw)
    unknown_aliases = {"unknown", "unknown_sub_state", "未知", "不确定"}
    if normalized == "unknown_sub_state" and raw not in unknown_aliases:
        raise ValueError(error_code)
    allowed_codes = (
        STATE_ONLY_OUTPUT_CODES
        if allow_overlays
        else STAGE2_MODEL_OUTPUT_STATE_CODES
    )
    if normalized not in allowed_codes:
        raise ValueError(error_code)
    return normalized


def _state_only_segment_state(
    sub_category: str,
    canonical_state: str,
) -> tuple[str, list[str]]:
    if sub_category in STATE_OVERLAY_CODES:
        compatible = OVERLAY_COMPATIBLE_PRIMARY_STATES.get(sub_category, ())
        if canonical_state == sub_category:
            raise ValueError("overlay_requires_primary_state")
        if compatible and canonical_state not in compatible:
            raise ValueError("sub_category_canonical_mismatch")
        return canonical_state, [sub_category]
    if sub_category != canonical_state:
        raise ValueError("sub_category_canonical_mismatch")
    return canonical_state, []


def _validate_state_only_schema(
    data: Any,
    candidate_sequences: list[int],
    context_only_sequences: list[int] = None,
) -> dict:
    if not isinstance(data, dict):
        return _schema_error("not_a_dict")
    forbidden = sorted(
        field for field in STATE_ONLY_FORBIDDEN_FIELDS if field in data
    )
    if forbidden:
        return _schema_error("stage2_state_only_forbidden_field", forbidden[0])
    for field in (
        "sub_category",
        "canonical_state",
        "confidence",
        "evidence_message_ids",
    ):
        if field not in data:
            return _schema_error("missing_top_level_field", field)
    schema_version = data.get("schema_version")
    if schema_version is not None and str(schema_version).strip() != STAGE2_SCHEMA_VERSION:
        return _schema_error("invalid_schema_version")

    candidate_set = set(candidate_sequences)
    if not candidate_set:
        return _schema_error("no_candidate_sequences")
    candidate_position = {
        sequence: index for index, sequence in enumerate(candidate_sequences)
    }
    try:
        sub_category = _normalize_state_only_code(
            data.get("sub_category"),
            "invalid_sub_category",
        )
        canonical_state = _normalize_state_only_code(
            data.get("canonical_state"),
            "invalid_canonical_state",
            allow_overlays=False,
        )
        segment_canonical, secondary_tags = _state_only_segment_state(
            sub_category,
            canonical_state,
        )
        if isinstance(data.get("confidence"), bool):
            raise ValueError("invalid_confidence")
        confidence = round(
            min(1.0, max(0.0, float(data.get("confidence")))),
            2,
        )
        raw_evidence = data.get("evidence_message_ids")
        evidence, _all_evidence, evidence_validation = (
            _normalize_candidate_evidence(
                raw_evidence,
                candidate_set=candidate_set,
                candidate_position=candidate_position,
                context_only_set=set(context_only_sequences or ()),
            )
        )
    except _EvidenceValidationError as exc:
        return _schema_error(
            str(exc),
            evidence_validation=_evidence_validation_summary([exc.validation]),
        )
    except ValueError as exc:
        return _schema_error(str(exc))

    start_sequence = min(evidence, key=candidate_position.__getitem__)
    end_sequence = max(candidate_set)
    route = route_for_state_with_overlays(segment_canonical, secondary_tags)
    route_mode = str(route.get("route_mode") or "").strip()
    route_should_intervene = route_mode == "REQUIRED_INTERVENTION"
    inhibition_strategy_id = route.get("inhibition_strategy_id")
    candidate_ids = (
        list(route.get("candidate_strategy_ids") or [])
        if route_should_intervene and not inhibition_strategy_id
        else []
    )
    legacy_state = legacy_state_for_sub_state(segment_canonical)
    active_segment = {
        "raw_sub_state": str(data.get("sub_category") or sub_category).strip(),
        "raw_sub_state_code": str(data.get("sub_category") or sub_category).strip(),
        "canonical_sub_state": segment_canonical,
        "canonical_sub_state_code": segment_canonical,
        "secondary_tags": secondary_tags,
        "start_sequence": start_sequence,
        "end_sequence": end_sequence,
        "confidence": confidence,
        "evidence_message_ids": evidence,
        "evidence_sequences": evidence,
        "reason": "state-only detector result",
        "is_active_at_window_end": True,
        "detected_self_regulation": False,
        "state": legacy_state,
        "state_code": legacy_state,
        "segment_order": 0,
    }
    active_payload = {
        "raw_sub_state": active_segment["raw_sub_state"],
        "canonical_sub_state": segment_canonical,
        "secondary_tags": list(secondary_tags),
        "confidence": confidence,
        "start_sequence": start_sequence,
        "end_sequence": end_sequence,
        "evidence_message_ids": list(evidence),
        "detected_self_regulation": False,
    }
    state_recognition = {
        "sub_category": sub_category,
        "canonical_state": (
            canonical_state
            if canonical_state != sub_category
            else segment_canonical
        ),
        "confidence": confidence,
        "evidence_message_ids": list(evidence),
    }
    return _json_result(
        data={
            "schema_version": STAGE2_SCHEMA_VERSION,
            "analysis_scope": {
                "candidate_start_sequence": min(candidate_set),
                "candidate_end_sequence": max(candidate_set),
                "input_cutoff_student_sequence": max(candidate_set),
            },
            "segments": [active_segment],
            "active_sub_state": active_payload,
            "active_segment_index": 0,
            "fresh_detected_self_regulation": False,
            "should_intervene": route_should_intervene,
            "inhibition": {
                "is_inhibited": bool(inhibition_strategy_id),
                "strategy_id": inhibition_strategy_id,
                "reason": None,
            },
            "candidate_strategy_ids": candidate_ids,
            "decision_reason": "state-only detector result",
            "evidence_validation": _evidence_validation_summary(
                [evidence_validation]
            ),
            "state_recognition": state_recognition,
            "sub_category": state_recognition["sub_category"],
            "canonical_state": state_recognition["canonical_state"],
        }
    )


def _validate_stage2_output_schema(
    data: Any,
    candidate_sequences: list[int],
    context_only_sequences: list[int] = None,
) -> dict:
    if isinstance(data, dict) and "segments" not in data and "active_sub_state" not in data:
        return _validate_state_only_schema(
            data,
            candidate_sequences,
            context_only_sequences,
        )
    return _validate_multi_segment_schema(
        data,
        candidate_sequences,
        context_only_sequences,
    )


def _validate_stage2_segment(
    item: dict,
    *,
    index: int,
    candidate_set: set[int],
    candidate_position: dict[int, int],
    context_only_set: set[int],
    previous_end: Optional[int],
) -> dict:
    if not isinstance(item, dict):
        raise ValueError("invalid_segment")
    raw_sub_state = _bounded_text(item.get("raw_sub_state") or item.get("raw_sub_state_code"), 120)
    if not raw_sub_state:
        raise ValueError("missing_raw_sub_state")
    canonical = _canonical_from_payload(item)
    try:
        secondary_tags = _dedupe_strings(
            item.get("secondary_tags", []),
            allowed=set(STATE_OVERLAY_CODES),
            max_items=MAX_SECONDARY_TAGS,
        )
    except ValueError as exc:
        if str(exc) == "too_many_items":
            raise ValueError("too_many_secondary_tags") from exc
        raise ValueError("invalid_secondary_tag") from exc
    secondary_tags = [tag for tag in secondary_tags if tag != canonical]
    if any(
        canonical not in OVERLAY_COMPATIBLE_PRIMARY_STATES.get(tag, ())
        for tag in secondary_tags
    ):
        raise ValueError("secondary_tag_primary_mismatch")
    start = _required_int(item.get("start_sequence"), "invalid_start_sequence")
    end = _required_int(item.get("end_sequence"), "invalid_end_sequence")
    if start not in candidate_set or end not in candidate_set:
        raise ValueError("sequence_outside_candidate")
    if start > end:
        raise ValueError("invalid_segment_range")
    if previous_end is not None and start <= previous_end:
        raise ValueError("overlapping_or_unsorted_segments")

    raw_evidence = item.get("evidence_message_ids")
    if raw_evidence is None:
        raw_evidence = item.get("evidence_sequences")
    evidence, all_evidence, evidence_validation = _normalize_candidate_evidence(
        raw_evidence,
        candidate_set=candidate_set,
        candidate_position=candidate_position,
        context_only_set=context_only_set,
        segment_start=start,
        segment_end=end,
    )

    if isinstance(item.get("confidence"), bool):
        raise ValueError("invalid_confidence")
    try:
        confidence = float(item.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_confidence") from exc
    confidence = round(min(1.0, max(0.0, confidence)), 2)
    reason = _bounded_text(item.get("reason"), 500)
    active_value = item.get("is_active_at_window_end")
    if not isinstance(active_value, bool):
        raise ValueError("invalid_active_segment_flag")
    if active_value and end != max(candidate_set):
        raise ValueError("active_segment_not_at_window_end")

    legacy_state = legacy_state_for_sub_state(canonical)
    return {
        "raw_sub_state": raw_sub_state,
        "raw_sub_state_code": raw_sub_state,
        "canonical_sub_state": canonical,
        "canonical_sub_state_code": canonical,
        "secondary_tags": secondary_tags,
        "start_sequence": start,
        "end_sequence": end,
        "confidence": confidence,
        "evidence_message_ids": evidence,
        "evidence_sequences": evidence,
        "reason": reason,
        "is_active_at_window_end": bool(active_value),
        "detected_self_regulation": bool(item.get("detected_self_regulation")),
        "state": legacy_state,
        "state_code": legacy_state,
        "segment_order": index,
        "_candidate_position": candidate_position[start],
        "_all_evidence_message_ids": all_evidence,
        "_evidence_validation": evidence_validation,
    }


def _active_segment_index(segments: list[dict], active_payload: dict) -> Optional[int]:
    if not isinstance(active_payload, dict):
        raise ValueError("invalid_active_sub_state")
    active_canonical = _canonical_from_payload(active_payload)
    active_start = _required_int(active_payload.get("start_sequence"), "invalid_active_start_sequence")
    active_end = _required_int(active_payload.get("end_sequence"), "invalid_active_end_sequence")
    raw_evidence = active_payload.get("evidence_message_ids")
    if raw_evidence is None:
        raw_evidence = active_payload.get("evidence_sequences")
    # The active payload duplicates a segment that already owns the required
    # evidence. Some JSON-mode models omit only this duplicated list while
    # preserving the canonical state and exact range. Treat omission as "use
    # the matching segment evidence"; still reject an explicitly malformed
    # value and let the normalized result copy the validated segment evidence.
    if raw_evidence is None:
        raw_evidence = []
    elif not isinstance(raw_evidence, list):
        raise ValueError("invalid_active_evidence_message_ids")
    active_evidence = []
    for raw_sequence in raw_evidence:
        sequence = _required_int(raw_sequence, "invalid_active_evidence_message_id")
        if sequence not in active_evidence:
            active_evidence.append(sequence)
    for index, segment in enumerate(segments):
        if (
            segment["canonical_sub_state"] == active_canonical
            and segment["start_sequence"] == active_start
            and segment["end_sequence"] == active_end
            and set(active_evidence).issubset(
                set(segment["_all_evidence_message_ids"])
            )
        ):
            return index
    raise ValueError("active_sub_state_not_in_segments")


def _validate_stage2_routes(
    *,
    active_segment: dict,
    should_intervene: Any,
    inhibition: Any,
    candidate_strategy_ids: Any,
) -> tuple[bool, dict, list[str]]:
    route = route_for_state_with_overlays(
        active_segment["canonical_sub_state"],
        active_segment.get("secondary_tags"),
    )
    route_should_intervene = bool(route["should_intervene"])
    route_mode = str(route.get("route_mode") or "").strip()
    optional_support = bool(route.get("is_optional_support")) or route_mode == "OPTIONAL_SUPPORT"
    fresh_self_regulation = bool(
        active_segment.get("detected_self_regulation")
        and active_segment.get("evidence_message_ids")
    )
    expected_should = False if fresh_self_regulation else route_should_intervene
    inhibition_strategy_id = route["inhibition_strategy_id"]
    if not isinstance(should_intervene, bool):
        raise ValueError("invalid_should_intervene")
    # JSON-mode models commonly use null for this duplicated route object.
    # The route is a deterministic function of the already validated canonical
    # state, so null is unambiguous for both OI and non-OI states. Explicitly
    # malformed objects, conflicting flags, and wrong strategy IDs still fail.
    if inhibition is None:
        inhibition = {
            "is_inhibited": bool(inhibition_strategy_id),
            "strategy_id": inhibition_strategy_id,
            "reason": None,
        }
    if not isinstance(inhibition, dict):
        raise ValueError("invalid_inhibition")
    if optional_support and not fresh_self_regulation:
        expected_should = bool(should_intervene)
    if not fresh_self_regulation and not optional_support and should_intervene != expected_should:
        raise ValueError("should_intervene_route_mismatch")

    raw_ids = _dedupe_strings(candidate_strategy_ids)
    allowed_ids = set(route["candidate_strategy_ids"])
    invalid_ids = [item for item in raw_ids if item not in allowed_ids]
    if invalid_ids:
        raise ValueError("invalid_candidate_strategy_id")
    if (
        (route_should_intervene or (optional_support and should_intervene))
        and not fresh_self_regulation
        and not raw_ids
    ):
        raise ValueError("missing_candidate_strategy_ids")

    is_inhibited = inhibition.get("is_inhibited")
    if not isinstance(is_inhibited, bool):
        raise ValueError("invalid_inhibition_flag")
    raw_inhibition_strategy = inhibition.get("strategy_id")
    inhibition_id = str(raw_inhibition_strategy).strip() if raw_inhibition_strategy else None
    inhibition_reason = _bounded_text(inhibition.get("reason"), 500)
    if inhibition_strategy_id:
        if not is_inhibited or inhibition_id != inhibition_strategy_id:
            raise ValueError("oi_inhibition_required")
        if raw_ids and raw_ids != [inhibition_strategy_id]:
            raise ValueError("oi_candidate_strategy_mismatch")
        if should_intervene:
            raise ValueError("oi_should_not_intervene")
        normalized_candidate_ids = []
    else:
        if is_inhibited or inhibition_id:
            raise ValueError("unexpected_inhibition")
        if inhibition_id and inhibition_id in OI_STRATEGY_IDS:
            raise ValueError("unexpected_oi_strategy")
        normalized_candidate_ids = (
            []
            if fresh_self_regulation or (optional_support and not should_intervene)
            else raw_ids
        )

    normalized_inhibition = {
        "is_inhibited": bool(inhibition_strategy_id),
        "strategy_id": inhibition_strategy_id,
        "reason": inhibition_reason if inhibition_strategy_id else None,
    }
    return expected_should, normalized_inhibition, normalized_candidate_ids


def _validate_multi_segment_schema(
    data: Any,
    candidate_sequences: list[int],
    context_only_sequences: list[int] = None,
) -> dict:
    if not isinstance(data, dict):
        return _schema_error("not_a_dict")
    for field in (
        "schema_version",
        "analysis_scope",
        "segments",
        "active_sub_state",
        "should_intervene",
        "inhibition",
        "candidate_strategy_ids",
        "decision_reason",
    ):
        if field not in data:
            return _schema_error("missing_top_level_field", field)
    if str(data.get("schema_version") or "").strip() != STAGE2_SCHEMA_VERSION:
        return _schema_error("invalid_schema_version")
    if not isinstance(data.get("analysis_scope"), dict):
        return _schema_error("invalid_analysis_scope")
    raw_segments = data.get("segments")
    if not isinstance(raw_segments, list):
        return _schema_error("segments_must_be_list")
    if not raw_segments:
        return _schema_error("segments_required")
    if len(raw_segments) > int(STATE_LLM_MAX_SEGMENTS):
        return _schema_error("too_many_segments")

    candidate_set = set(candidate_sequences)
    if not candidate_set:
        return _schema_error("no_candidate_sequences")
    scope = data.get("analysis_scope") or {}
    try:
        scope_start = _required_int(
            scope.get("candidate_start_sequence"),
            "invalid_analysis_scope",
        )
        scope_end = _required_int(
            scope.get("candidate_end_sequence"),
            "invalid_analysis_scope",
        )
        scope_cutoff = _required_int(
            scope.get("input_cutoff_student_sequence"),
            "invalid_analysis_scope",
        )
    except ValueError as exc:
        return _schema_error(str(exc))
    if scope_start != min(candidate_set) or scope_end != max(candidate_set) or scope_cutoff != max(candidate_set):
        return _schema_error("analysis_scope_mismatch")
    candidate_position = {sequence: index for index, sequence in enumerate(candidate_sequences)}
    context_only_set = set(context_only_sequences or ())
    normalized = []
    previous_end = None
    try:
        for index, item in enumerate(raw_segments):
            segment = _validate_stage2_segment(
                item,
                index=index,
                candidate_set=candidate_set,
                candidate_position=candidate_position,
                context_only_set=context_only_set,
                previous_end=previous_end,
            )
            normalized.append(segment)
            previous_end = segment["end_sequence"]
        active_flags = [index for index, item in enumerate(normalized) if item["is_active_at_window_end"]]
        if len(active_flags) > 1:
            return _schema_error("multiple_active_segments")
        active_payload = data.get("active_sub_state")
        # ``active_sub_state`` duplicates one fully validated segment. JSON-mode
        # repair responses sometimes preserve that segment and its explicit
        # window-end flag but serialize only the duplicate field as null. This
        # is unambiguous when exactly one segment is marked active; keep failing
        # closed when there are zero or multiple candidates.
        if not isinstance(active_payload, dict) and len(active_flags) == 1:
            active_index = active_flags[0]
        else:
            active_index = _active_segment_index(normalized, active_payload)
        if active_flags and active_flags[0] != active_index:
            return _schema_error("active_segment_mismatch")
        normalized[active_index]["is_active_at_window_end"] = True
        active_segment = normalized[active_index]
        if active_segment["end_sequence"] != max(candidate_set):
            return _schema_error("active_segment_not_at_window_end")
        should_intervene, inhibition, candidate_ids = _validate_stage2_routes(
            active_segment=active_segment,
            should_intervene=data.get("should_intervene"),
            inhibition=data.get("inhibition"),
            candidate_strategy_ids=data.get("candidate_strategy_ids"),
        )
    except _EvidenceValidationError as exc:
        return _schema_error(
            str(exc),
            evidence_validation=_evidence_validation_summary([exc.validation]),
        )
    except ValueError as exc:
        return _schema_error(str(exc))

    active_payload = {
        "raw_sub_state": active_segment["raw_sub_state"],
        "canonical_sub_state": active_segment["canonical_sub_state"],
        "secondary_tags": list(active_segment["secondary_tags"]),
        "confidence": active_segment["confidence"],
        "start_sequence": active_segment["start_sequence"],
        "end_sequence": active_segment["end_sequence"],
        "evidence_message_ids": list(active_segment["evidence_message_ids"]),
        "detected_self_regulation": bool(active_segment["detected_self_regulation"]),
    }
    public_segments = []
    evidence_validations = []
    for segment in normalized:
        clean = dict(segment)
        clean.pop("_candidate_position", None)
        clean.pop("_all_evidence_message_ids", None)
        evidence_validation = clean.pop("_evidence_validation", None)
        if evidence_validation is not None:
            evidence_validations.append(evidence_validation)
        public_segments.append(clean)

    return _json_result(
        data={
            "schema_version": STAGE2_SCHEMA_VERSION,
            "analysis_scope": {
                "candidate_start_sequence": min(candidate_set),
                "candidate_end_sequence": max(candidate_set),
                "input_cutoff_student_sequence": max(candidate_set),
            },
            "segments": public_segments,
            "active_sub_state": active_payload,
            "active_segment_index": active_index,
            "fresh_detected_self_regulation": bool(
                active_segment["detected_self_regulation"]
                and active_segment["evidence_message_ids"]
            ),
            "should_intervene": should_intervene,
            "inhibition": inhibition,
            "candidate_strategy_ids": candidate_ids,
            "decision_reason": _bounded_text(data.get("decision_reason"), 800),
            "evidence_validation": _evidence_validation_summary(
                evidence_validations
            ),
        }
    )


def parse_llm_json_content(
    content: Any,
    candidate_sequences: list[int] = None,
    context_only_sequences: list[int] = None,
) -> dict:
    """Parse and validate the Stage 2 state-only JSON contract."""
    if content is None:
        return _schema_error("empty_content")
    if isinstance(content, dict):
        return _validate_stage2_output_schema(
            content,
            list(candidate_sequences or []),
            list(context_only_sequences or []),
        )
    if not isinstance(content, str) or not content.strip():
        return _schema_error("empty_content")
    objects, structurally_incomplete = _json_object_candidates(content)
    if not objects:
        if structurally_incomplete:
            return _schema_error("truncated_response")
        return _schema_error("json_parse_error", "no complete JSON object found")

    # A response may contain an example object before the actual answer.  Try
    # each complete object and accept the first one that satisfies the
    # contract; never merge fields across objects.
    first_error = None
    for data in objects:
        parsed = _validate_stage2_output_schema(
            data,
            list(candidate_sequences or []),
            list(context_only_sequences or []),
        )
        if parsed["valid"]:
            return parsed
        if first_error is None:
            first_error = parsed
    return first_error or _schema_error("json_parse_error")


def _replay_model_content(response: Any) -> Any:
    """Extract only assistant content from a stored response envelope."""

    if response is None:
        return None
    if hasattr(response, "output") or hasattr(response, "raw_text"):
        raw_output = _raw_output(response)
        return _stage2_model_content(response, raw_output)

    envelope = response
    if isinstance(response, str):
        try:
            envelope = json.loads(response)
        except (TypeError, ValueError):
            envelope = None
    if isinstance(envelope, dict) and envelope.get("choices"):
        message = (envelope.get("choices") or [{}])[0].get("message") or {}
        if isinstance(message, dict) and "content" in message:
            content = message.get("content")
            if isinstance(content, list):
                return "".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict)
                )
            return content
    return response


def _replay_response_shape(content: Any, finish_reason: Any = None) -> dict:
    if isinstance(content, str):
        text = content.strip()
    elif content is None:
        text = ""
    else:
        try:
            text = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(content)
    candidates, structurally_incomplete = _json_object_candidates(text)
    return {
        "available": content is not None,
        "response_character_count": len(text),
        "finish_reason": finish_reason,
        "json_extractable": bool(candidates),
        "open_brace_present": "{" in text,
        "close_brace_present": "}" in text,
        "response_incomplete": bool(
            structurally_incomplete
            or str(finish_reason or "").strip().lower() == "length"
        ),
    }


def _replay_parse_summary(parsed: dict) -> dict:
    summary = {
        "valid": bool(parsed and parsed.get("valid")),
        "error_type": parsed.get("error_type") if parsed else "empty_content",
        "error_message": parsed.get("error_message") if parsed else "empty_content",
    }
    if not summary["valid"]:
        return summary
    data = parsed.get("data") or {}
    recognition = data.get("state_recognition") or {}
    active = data.get("active_sub_state") or {}
    summary.update(
        {
            "sub_category": recognition.get("sub_category"),
            "canonical_state": recognition.get("canonical_state")
            or active.get("canonical_sub_state"),
            "confidence": recognition.get("confidence")
            if recognition.get("confidence") is not None
            else active.get("confidence"),
            "evidence_message_ids": list(
                recognition.get("evidence_message_ids")
                or active.get("evidence_message_ids")
                or []
            ),
        }
    )
    summary.pop("error_type", None)
    summary.pop("error_message", None)
    return summary


def replay_stage2_response(
    initial_response: Any,
    *,
    repair_response: Any = None,
    candidate_sequences: list[int] = None,
    initial_finish_reason: Any = None,
    repair_finish_reason: Any = None,
) -> dict:
    """Replay Stage 2 parsing without a gateway or any persistence side effect."""

    candidates = list(candidate_sequences or [])
    initial_content = _replay_model_content(initial_response)
    initial_shape = _replay_response_shape(initial_content, initial_finish_reason)
    initial_parse = parse_llm_json_content(initial_content, candidates)
    final_parse = initial_parse
    repair_summary = {
        "status": "not_requested" if initial_parse.get("valid") else "not_available",
        "reason": None
        if initial_parse.get("valid")
        else "repair_response_not_provided",
    }

    if not initial_parse.get("valid") and repair_response is not None:
        repair_content = _replay_model_content(repair_response)
        repair_shape = _replay_response_shape(repair_content, repair_finish_reason)
        repair_parse = parse_llm_json_content(repair_content, candidates)
        repair_summary = {
            "status": "parsed",
            "response": repair_shape,
            "parse": _replay_parse_summary(repair_parse),
        }
        final_parse = repair_parse

    final_shape = (
        repair_summary.get("response")
        if repair_summary.get("status") == "parsed"
        else initial_shape
    )
    final_failure_category = None
    if not final_parse.get("valid"):
        final_failure_category = normalize_stage2_failure(
            final_parse.get("error_type"),
            finish_reason=(
                repair_finish_reason
                if repair_summary.get("status") == "parsed"
                else initial_finish_reason
            ),
            response_incomplete=bool(final_shape.get("response_incomplete")),
        )

    return {
        "read_only": True,
        "external_call_count": 0,
        "initial": {"response": initial_shape},
        "local_parse": _replay_parse_summary(initial_parse),
        "repair": repair_summary,
        "final": {
            "status": "success" if final_parse.get("valid") else "failed",
            "failure_category": final_failure_category,
            "parse": _replay_parse_summary(final_parse),
        },
        "side_effects": {
            "database_writes": 0,
            "pipeline_created": False,
            "agent_messages_published": False,
        },
    }


def _failure_type(schema_error: str) -> str:
    normalized = str(schema_error or "").strip().lower()
    if normalized in {
        "read_timeout",
        "connect_timeout",
        "truncated_response",
        "reasoning_budget_exhausted",
        "json_parse_error",
        "schema_validation_error",
    }:
        return normalized
    if normalized in {"empty_response", "no_output"}:
        return "empty_response"
    if normalized in {
        "authentication_error",
        "rate_limited",
        "network_error",
        "upstream_5xx",
        "llm_error",
        "unknown_error",
    }:
        return "llm_transport_error"
    return "schema_validation_error"


def _exception_failure_type(exc: Exception) -> str:
    name = exc.__class__.__name__.lower()
    if "readtimeout" in name or "read_timeout" in name:
        return "read_timeout"
    if "connecttimeout" in name or "connect_timeout" in name:
        return "connect_timeout"
    if "timeout" in name:
        return "connect_timeout"
    if isinstance(exc, json.JSONDecodeError):
        return "json_parse_error"
    return "application_error"


def _raw_output(result) -> str:
    if result is None:
        return ""
    output = getattr(result, "output", None)
    if isinstance(output, str):
        return output
    if output is not None:
        return json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    return str(getattr(result, "raw_text", None) or "")


def _compatibility_projection(data: dict, sequence_to_message_id: dict) -> dict:
    projected = dict(data)
    active_index = projected.get("active_segment_index")
    active = None
    if isinstance(active_index, int) and 0 <= active_index < len(projected.get("segments") or []):
        active = projected["segments"][active_index]
    canonical = (active or {}).get("canonical_sub_state") or "unknown_sub_state"
    state = legacy_state_for_sub_state(canonical)
    legacy_state = _LEGACY_STATE_CODES.get(state, state)
    evidence_sequences = list((active or {}).get("evidence_message_ids") or [])
    projected.update(
        {
            "primary_state": legacy_state,
            "state_code": legacy_state,
            "confidence": float((active or {}).get("confidence") or 0.0),
            "evidence_message_ids": [
                sequence_to_message_id[sequence]
                for sequence in evidence_sequences
                if sequence in sequence_to_message_id
            ],
            "secondary_state": None,
            "reason": projected.get("decision_reason"),
            "self_regulation_detected": bool(
                (active or {}).get("detected_self_regulation")
                or any(
                    bool(item.get("detected_self_regulation"))
                    for item in (projected.get("segments") or [])
                    if isinstance(item, dict)
                )
            ),
            "should_intervene_recommendation": bool(projected.get("should_intervene")),
            "stage2_schema_version": projected.get("schema_version"),
        }
    )
    return projected


class LLMStateDetector:
    DETECTOR_VERSION = f"{PIPELINE_V2_ANALYZER_VERSION}_stage2_state_only_v10"
    STATE_DETECTOR_PROFILE = "state_detector"

    @staticmethod
    def _build_call_payload(messages: list[dict]) -> dict:
        return {
            "messages": messages,
            "max_tokens": int(STATE_LLM_OUTPUT_MAX_TOKENS),
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "_sera_final_content_only": True,
            "_sera_compatibility_fallback_fields": ["thinking"],
        }

    @staticmethod
    def _build_retry_payload(messages: list[dict]) -> dict:
        return {
            "messages": messages,
            "temperature": STATE_DETECTOR_RETRY_TEMPERATURE,
            "max_tokens": int(STATE_LLM_REPAIR_OUTPUT_MAX_TOKENS),
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "_sera_final_content_only": True,
            "_sera_compatibility_fallback_fields": ["thinking"],
        }

    @staticmethod
    def _build_fallback_result(
        error_type: str,
        error_message: str,
        *,
        schema_error: str = None,
        attempt_count: int = 1,
        previous_meta: dict = None,
        failure_category: str = None,
        pipeline_run_id: int = None,
        assessment_batch_id: int = None,
    ) -> dict:
        result = {
            "segments": [],
            "active_segment_index": None,
            "intervention": {
                "needed": False,
                "target_segment_index": None,
                "reason_code": "insufficient_evidence",
                "message": None,
            },
            "primary_state": "unknown",
            "state_code": "unknown",
            "confidence": 0.0,
            "evidence_message_ids": [],
            "secondary_state": None,
            "reason": error_message,
            "detector_error": True,
            "error_type": error_type,
        }
        meta = {
            "analysis_skipped": False,
            "analysis_failed": True,
            "llm_required": True,
            "profile": LLMStateDetector.STATE_DETECTOR_PROFILE,
            "prompt_version": LLMStateDetector.DETECTOR_VERSION,
            "success": False,
            "failure_type": error_type,
            "failure_reason": error_type,
            "failure_message": error_message,
            "fallback_required": True,
            "attempt_count": attempt_count,
            "retry_count": max(0, attempt_count - 1),
            "max_attempts": int(STATE_LLM_SCHEMA_MAX_ATTEMPTS),
            "schema_valid": False,
            "validation_status": "failed",
            "schema_error": schema_error or error_type,
            "failure_category": failure_category,
            "pipeline_run_id": pipeline_run_id,
            "assessment_batch_id": assessment_batch_id,
            "external_call_count": attempt_count,
            "repair_attempt_count": max(0, attempt_count - 1),
            "gateway_retry_count": 0,
            "validation_attempts": [],
        }
        if previous_meta:
            meta.update(previous_meta)
            meta.update(
                {
                    "success": False,
                    "failure_type": error_type,
                    "failure_reason": error_type,
                    "failure_message": error_message,
                    "schema_valid": False,
                    "validation_status": "failed",
                    "schema_error": schema_error or error_type,
                    "failure_category": failure_category
                    or previous_meta.get("failure_category"),
                }
            )
        return {"result": result, "meta": meta}

    @staticmethod
    def detect(context: dict, rule_assessment: dict = None, features: dict = None) -> dict:
        if not SERA_LLM_ENABLED:
            return {
                "result": None,
                "meta": {
                    "analysis_skipped": True,
                    "success": False,
                    "skip_reason": "llm_disabled",
                    "profile": LLMStateDetector.STATE_DETECTOR_PROFILE,
                    "model_name": None,
                    "prompt_version": LLMStateDetector.DETECTOR_VERSION,
                },
            }

        split = _split_detector_messages(context or {})
        if not split["candidate_messages"]:
            return {
                "result": None,
                "meta": {
                    "analysis_skipped": True,
                    "success": False,
                    "skip_reason": "no_candidate_student_messages",
                    "profile": LLMStateDetector.STATE_DETECTOR_PROFILE,
                    "prompt_version": LLMStateDetector.DETECTOR_VERSION,
                },
            }

        current_task = context.get("current_task") or {}
        user_context = {
            "schema_version": STAGE2_SCHEMA_VERSION,
            "analysis_scope": {
                "candidate_start_sequence": split["candidate_sequences"][0],
                "candidate_end_sequence": split["candidate_sequences"][-1],
                "input_cutoff_student_sequence": split["candidate_sequences"][-1],
                "group_id": context.get("group_id"),
                "session_id": context.get("session_id"),
                "discussion_id": context.get("discussion_id"),
                "trigger_source": context.get("trigger_type"),
                "is_explicit_help": context.get("trigger_type")
                in {"student_help", "student_help_request", "help_request"},
            },
            "current_task": {
                "title": _bounded_text(current_task.get("title"), 200),
                "core_question": _bounded_text(
                    current_task.get("question") or current_task.get("core_question"), 600
                ),
                "phase": _bounded_text(current_task.get("phase"), 100),
                "goal": _bounded_text(
                    current_task.get("requirements") or current_task.get("goal"), 800
                ),
            },
            "context_only": split["context_only"],
            "candidate_messages": split["candidate_messages"],
            "evidence_contract": {
                "allowed_candidate_sequences": list(
                    split["candidate_sequences"]
                ),
                "context_only_sequences": list(split["context_sequences"]),
                "context_only_may_be_evidence": False,
                "mixed_evidence_policy": (
                    "retain_valid_candidate_sequences_and_audit_rejections"
                ),
                "minimum_valid_candidate_evidence": 1,
                "sequence_inference_allowed": False,
            },
            "recent_strategy_intervention": {
                key: _bounded_text((context.get("recent_intervention") or {}).get(key), 300)
                for key in (
                    "content",
                    "message",
                    "created_at",
                    "sequence",
                    "pipeline_run_id",
                    "published_message_id",
                    "canonical_sub_state_code",
                )
                if (context.get("recent_intervention") or {}).get(key) is not None
            },
            "post_intervention_observation": context.get("post_intervention_observation"),
            "recent_state_summary": _bounded_text(context.get("recent_state"), 300),
            "rule_hints": [
                {
                    "state": item.get("state_code"),
                    "score": item.get("score"),
                }
                for item in (rule_assessment or {}).get("candidates", [])[:3]
            ],
            "stage1_result": context.get("stage1_result"),
            "allowed_sub_categories": list(STATE_ONLY_OUTPUT_CODES),
            "allowed_canonical_sub_states": list(STAGE2_MODEL_OUTPUT_STATE_CODES),
            "sub_category_to_compatible_canonical_states": {
                code: list(compatible_states)
                for code, compatible_states in (
                    SUB_CATEGORY_CANONICAL_COMPATIBILITY.items()
                )
            },
            "allowed_secondary_tags": list(STATE_OVERLAY_CODES),
            "secondary_tag_semantics": STATE_OVERLAY_SEMANTICS,
            "state_boundary_guidance": STAGE2_STATE_BOUNDARY_GUIDANCE,
            "canonical_sub_state_semantics": {
                code: CANONICAL_SUB_STATE_SEMANTICS[code]
                for code in STAGE2_MODEL_OUTPUT_STATE_CODES
            },
            "limits": {
                "max_evidence_per_segment": int(STATE_LLM_MAX_EVIDENCE_PER_SEGMENT),
            },
            "output_contract": {
                "schema_version": STAGE2_SCHEMA_VERSION,
                "no_student_visible_text": True,
                "evidence_message_ids_are_candidate_sequences": True,
                "allowed_evidence_sequences": list(
                    split["candidate_sequences"]
                ),
                "forbidden_context_only_sequences": list(
                    split["context_sequences"]
                ),
                "state_only": True,
                "required_top_level_fields": [
                    "sub_category",
                    "canonical_state",
                    "confidence",
                    "evidence_message_ids",
                ],
                "forbidden_top_level_fields": [
                    "should_intervene",
                    "inhibition",
                    "candidate_strategy_ids",
                    "strategy_id",
                    "selected_strategy_id",
                    "strategy_pool",
                    "intervention",
                    "intervention_message",
                    "message",
                ],
                "burnout_must_remain_burnout": True,
                "stage_achievement_sub_category_uses_execution_progress_canonical_state": True,
                "overlay_requires_explicit_primary_canonical_state": True,
            },
        }
        gateway = get_gateway()
        max_attempts = max(1, int(STATE_LLM_SCHEMA_MAX_ATTEMPTS))
        pipeline_run_id = _resolve_stage2_pipeline_run_id(context or {})
        assessment_batch_id = (context or {}).get("assessment_batch_id")
        audit_context = dict(context or {})
        if pipeline_run_id is not None:
            audit_context["pipeline_run_id"] = pipeline_run_id
        validation_attempts = []
        last_error = "state detector returned no valid JSON"
        last_schema_error = "no_output"
        last_raw_output = ""
        last_meta = {}
        external_call_count = 0
        gateway_retry_count = 0
        compatibility_fallback_count = 0

        try:
            for attempt_index in range(max_attempts):
                remaining_external_call_budget = max_attempts - external_call_count
                if remaining_external_call_budget <= 0:
                    break
                retry_context = dict(user_context)
                if attempt_index:
                    retry_context["previous_output"] = last_raw_output[:240]
                    retry_context["validation_error"] = last_schema_error
                messages = [
                    {
                        "role": "system",
                        "content": (
                            _STATE_DETECTOR_SYSTEM_PROMPT
                            if attempt_index == 0
                            else _RETRY_STATE_DETECTOR_SYSTEM_PROMPT
                        ),
                    },
                    {"role": "user", "content": json.dumps(retry_context, ensure_ascii=False)},
                ]
                payload = (
                    LLMStateDetector._build_call_payload(messages)
                    if attempt_index == 0
                    else LLMStateDetector._build_retry_payload(messages)
                )
                payload["_sera_external_call_budget"] = (
                    remaining_external_call_budget
                )
                attempt_type = "repair" if attempt_index else "initial"
                call_id = str(uuid.uuid4())
                call_timer = latency_timer()
                profile = getattr(gateway, "profiles", {}).get(
                    LLMStateDetector.STATE_DETECTOR_PROFILE
                )
                try:
                    profile_retries = int(getattr(profile, "retries", 0))
                except (TypeError, ValueError):
                    profile_retries = 0
                prompt_metrics = _stage2_prompt_metrics(payload)
                call_details = {
                    "prompt_version": LLMStateDetector.DETECTOR_VERSION,
                    "attempt_type": attempt_type,
                    "gateway_max_attempts": max(1, profile_retries + 1),
                    "external_call_budget": max_attempts,
                    "max_tokens": payload.get("max_tokens"),
                    "model": getattr(profile, "model", None),
                    "profile": LLMStateDetector.STATE_DETECTOR_PROFILE,
                    "timeout": getattr(profile, "read_timeout", None),
                    "timeout_seconds": getattr(profile, "read_timeout", None),
                    "gateway_retries": profile_retries,
                    **prompt_metrics,
                    "gateway_retry_count": 0,
                    "compatibility_fallback_count": 0,
                    "batch_scheduler_attempt_count": (context or {}).get(
                        "batch_scheduler_attempt_count"
                    ),
                    "batch_scheduler_max_attempts": (context or {}).get(
                        "batch_scheduler_max_attempts"
                    ),
                    "detector_attempt_count": attempt_index + 1,
                    "stage2_attempt_count": attempt_index + 1,
                    "stage2_external_call_count": external_call_count + 1,
                    "stage2_repair_attempt_count": attempt_index,
                    "entered_repair": bool(attempt_index),
                }
                call_event = (
                    "stage2_repair"
                    if attempt_index
                    else f"stage2_llm_attempt_{attempt_index + 1}"
                )
                record_latency_event(
                    stage="stage2",
                    event=f"{call_event}_started",
                    pipeline_run_id=pipeline_run_id,
                    assessment_batch_id=assessment_batch_id,
                    call_id=call_id,
                    attempt=attempt_index + 1,
                    details=call_details,
                    pipeline_context=True,
                )

                gateway_result = None
                try:
                    gateway_result = gateway.call(
                        LLMStateDetector.STATE_DETECTOR_PROFILE, payload, "json"
                    )
                except Exception as exc:
                    external_call_count += 1
                    error_type = _exception_failure_type(exc)
                    last_schema_error = error_type
                    last_error = str(exc)[:200]
                    attempt_diagnostics = _stage2_attempt_diagnostics(
                        payload=payload,
                        gateway=gateway,
                        result=None,
                        raw_output="",
                        failure_reason=error_type,
                        error_message=last_error,
                        exception=True,
                    )
                    attempt_diagnostics["detector_attempt_count"] = attempt_index + 1
                    attempt_diagnostics["failure_category"] = normalize_stage2_failure(
                        error_type,
                        attempt_type=attempt_type,
                        error_message=last_error,
                        response_incomplete=attempt_diagnostics["response_incomplete"],
                        exception=True,
                    )
                    validation_attempts.append(
                        {
                            "attempt": attempt_index + 1,
                            "error_type": error_type,
                            "schema_error": error_type,
                            "error_message": last_error,
                            "attempt_type": attempt_type,
                            "valid": False,
                            "entered_repair": bool(attempt_index),
                            "raw_text_length": 0,
                            **attempt_diagnostics,
                        }
                    )
                    last_meta = {
                        **attempt_diagnostics,
                        "latency_ms": None,
                        "model_name": attempt_diagnostics.get("model"),
                        "raw_text_length": 0,
                        "raw_response": _safe_response_summary(attempt_diagnostics),
                        "token_usage": None,
                        "response_format": payload.get("response_format"),
                        "validation_attempts": list(validation_attempts),
                        "candidate_sequences": split["candidate_sequences"],
                        "context_only_sequences": split["context_sequences"],
                        "pipeline_run_id": pipeline_run_id,
                        "assessment_batch_id": assessment_batch_id,
                        "batch_scheduler_attempt_count": (context or {}).get(
                            "batch_scheduler_attempt_count"
                        ),
                        "batch_scheduler_max_attempts": (context or {}).get(
                            "batch_scheduler_max_attempts"
                        ),
                        "external_call_count": external_call_count,
                        "repair_attempt_count": sum(
                            1
                            for item in validation_attempts
                            if item.get("attempt_type") == "repair"
                        ),
                        "gateway_retry_count": gateway_retry_count,
                        "compatibility_fallback_count": compatibility_fallback_count,
                    }
                    _write_llm_audit(
                        audit_context,
                        None,
                        failure=error_type,
                        diagnostics=attempt_diagnostics,
                    )
                    record_latency_event(
                        stage="stage2",
                        event=f"{call_event}_finished",
                        pipeline_run_id=pipeline_run_id,
                        assessment_batch_id=assessment_batch_id,
                        call_id=call_id,
                        attempt=attempt_index + 1,
                        elapsed=elapsed_ms(call_timer),
                        details={
                            **call_details,
                            **attempt_diagnostics,
                            "success": False,
                            "failure_type": error_type,
                            "failure_category": attempt_diagnostics["failure_category"],
                            "stage2_failure_category": attempt_diagnostics[
                                "failure_category"
                            ],
                        },
                        pipeline_context=True,
                    )
                    break

                try:
                    gateway_http_attempt_count = max(
                        1,
                        int(getattr(gateway_result, "attempt_count", 1) or 1),
                    )
                except (TypeError, ValueError):
                    gateway_http_attempt_count = 1
                gateway_http_attempt_count = min(
                    remaining_external_call_budget,
                    gateway_http_attempt_count,
                )
                external_call_count += gateway_http_attempt_count
                raw_gateway_output = _raw_output(gateway_result)
                model_content = _stage2_model_content(
                    gateway_result,
                    raw_gateway_output,
                )
                if isinstance(model_content, str):
                    last_raw_output = model_content[:600]
                elif model_content is None:
                    last_raw_output = ""
                else:
                    try:
                        last_raw_output = json.dumps(
                            model_content,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )[:600]
                    except (TypeError, ValueError):
                        last_raw_output = str(model_content)[:600]

                if gateway_result and _stage2_should_locally_parse(
                    gateway_result,
                    model_content,
                ):
                    parsed = parse_llm_json_content(
                        model_content,
                        candidate_sequences=split["candidate_sequences"],
                        context_only_sequences=split["context_sequences"],
                    )
                    attempt_diagnostics = _stage2_attempt_diagnostics(
                        payload=payload,
                        gateway=gateway,
                        result=gateway_result,
                        raw_output=raw_gateway_output,
                        parser_result=parsed,
                    )
                    attempt_diagnostics["detector_attempt_count"] = attempt_index + 1
                    attempt_diagnostics["accepted_after_local_parse"] = bool(
                        parsed["valid"] and not gateway_result.success
                    )
                    if parsed["valid"]:
                        attempt_diagnostics["failure_category"] = None
                    else:
                        attempt_diagnostics["failure_category"] = normalize_stage2_failure(
                            parsed.get("error_type"),
                            finish_reason=getattr(gateway_result, "finish_reason", None),
                            attempt_type=attempt_type,
                            error_message=parsed.get("error_message"),
                            response_incomplete=attempt_diagnostics[
                                "response_incomplete"
                            ],
                        )
                    gateway_retry_count += int(
                        attempt_diagnostics.get("gateway_retry_count") or 0
                    )
                    compatibility_fallback_count += int(
                        attempt_diagnostics.get("compatibility_fallback_count") or 0
                    )
                    error_type = (
                        parsed.get("error_type")
                        if not parsed["valid"]
                        else None
                    )
                    error_message = (
                        parsed.get("error_message")
                        if not parsed["valid"]
                        else None
                    )
                    validation_attempts.append(
                        {
                            "attempt": attempt_index + 1,
                            "error_type": error_type,
                            "schema_error": error_type,
                            "error_message": str(error_message or "")[:200] or None,
                            "attempt_type": attempt_type,
                            "valid": bool(parsed["valid"]),
                            "entered_repair": bool(attempt_index),
                            "raw_text_length": len(last_raw_output),
                            **attempt_diagnostics,
                        }
                    )
                    _write_llm_audit(
                        audit_context,
                        gateway_result,
                        failure=error_type,
                        diagnostics=attempt_diagnostics,
                    )
                    record_latency_event(
                        stage="stage2",
                        event=f"{call_event}_finished",
                        pipeline_run_id=pipeline_run_id,
                        assessment_batch_id=assessment_batch_id,
                        call_id=call_id,
                        attempt=attempt_index + 1,
                        elapsed=elapsed_ms(call_timer),
                        details={
                            **call_details,
                            **attempt_diagnostics,
                            "success": bool(parsed["valid"]),
                            "failure_type": error_type,
                            "failure_category": attempt_diagnostics["failure_category"],
                            "stage2_failure_category": attempt_diagnostics[
                                "failure_category"
                            ],
                        },
                        pipeline_context=True,
                    )
                    last_meta = {
                        **attempt_diagnostics,
                        "latency_ms": getattr(gateway_result, "latency_ms", None),
                        "model_name": attempt_diagnostics.get("model"),
                        "raw_text_length": len(last_raw_output),
                        "raw_response": _safe_response_summary(attempt_diagnostics),
                        "token_usage": getattr(gateway_result, "token_usage", None),
                        "response_format": payload.get("response_format"),
                        "validation_attempts": list(validation_attempts),
                        "candidate_sequences": split["candidate_sequences"],
                        "context_only_sequences": split["context_sequences"],
                        "pipeline_run_id": pipeline_run_id,
                        "assessment_batch_id": assessment_batch_id,
                        "batch_scheduler_attempt_count": (context or {}).get(
                            "batch_scheduler_attempt_count"
                        ),
                        "batch_scheduler_max_attempts": (context or {}).get(
                            "batch_scheduler_max_attempts"
                        ),
                        "external_call_count": external_call_count,
                        "repair_attempt_count": sum(
                            1
                            for item in validation_attempts
                            if item.get("attempt_type") == "repair"
                        ),
                        "gateway_retry_count": gateway_retry_count,
                        "compatibility_fallback_count": compatibility_fallback_count,
                    }
                    if parsed["valid"]:
                        parsed["data"] = _enrich_explicit_overlay_tags(
                            parsed["data"], split["candidate_messages"]
                        )
                        projected = _compatibility_projection(
                            parsed["data"], split["sequence_to_message_id"]
                        )
                        return {
                            "result": projected,
                            "meta": {
                                **last_meta,
                                "analysis_skipped": False,
                                "analysis_failed": False,
                                "llm_required": True,
                                "profile": LLMStateDetector.STATE_DETECTOR_PROFILE,
                                "prompt_version": LLMStateDetector.DETECTOR_VERSION,
                                "attempt_count": attempt_index + 1,
                                "retry_count": attempt_index,
                                "max_attempts": max_attempts,
                                "success": True,
                                "failure_type": None,
                                "failure_message": None,
                                "fallback_required": False,
                                "retry_used": attempt_index > 0,
                                 "schema_valid": True,
                                 "validation_status": "passed",
                                 "schema_error": None,
                                 "failure_category": None,
                                 "validation_attempts": list(validation_attempts),
                                 "detector_attempt_count": attempt_index + 1,
                                 "external_call_count": external_call_count,
                                 "repair_attempt_count": sum(
                                     1
                                     for item in validation_attempts
                                     if item.get("attempt_type") == "repair"
                                 ),
                                 "gateway_retry_count": gateway_retry_count,
                                 "compatibility_fallback_count": compatibility_fallback_count,
                              },
                         }
                    last_schema_error = parsed.get("error_type") or "schema_validation_error"
                    last_error = parsed.get("error_message") or last_schema_error
                    error_type = _failure_type(last_schema_error)
                else:
                    error_type = getattr(gateway_result, "failure_type", None) or "llm_error"
                    last_schema_error = error_type
                    last_error = getattr(gateway_result, "failure_message", None) or "LLM call failed"
                    attempt_diagnostics = _stage2_attempt_diagnostics(
                        payload=payload,
                        gateway=gateway,
                        result=gateway_result,
                        raw_output=raw_gateway_output,
                        failure_reason=error_type,
                        error_message=last_error,
                    )
                    attempt_diagnostics["detector_attempt_count"] = attempt_index + 1
                    attempt_diagnostics["failure_category"] = normalize_stage2_failure(
                        error_type,
                        finish_reason=getattr(gateway_result, "finish_reason", None),
                        attempt_type=attempt_type,
                        error_message=last_error,
                        response_incomplete=attempt_diagnostics["response_incomplete"],
                    )
                    gateway_retry_count += int(
                        attempt_diagnostics.get("gateway_retry_count") or 0
                    )
                    compatibility_fallback_count += int(
                        attempt_diagnostics.get("compatibility_fallback_count") or 0
                    )
                    validation_attempts.append(
                        {
                            "attempt": attempt_index + 1,
                            "error_type": error_type,
                            "schema_error": last_schema_error,
                            "error_message": str(last_error)[:200],
                            "attempt_type": attempt_type,
                            "valid": False,
                            "entered_repair": bool(attempt_index),
                            "raw_text_length": len(last_raw_output),
                            **attempt_diagnostics,
                        }
                    )
                    _write_llm_audit(
                        audit_context,
                        gateway_result,
                        failure=error_type,
                        diagnostics=attempt_diagnostics,
                    )
                    record_latency_event(
                        stage="stage2",
                        event=f"{call_event}_finished",
                        pipeline_run_id=pipeline_run_id,
                        assessment_batch_id=assessment_batch_id,
                        call_id=call_id,
                        attempt=attempt_index + 1,
                        elapsed=elapsed_ms(call_timer),
                        details={
                            **call_details,
                            **attempt_diagnostics,
                            "success": False,
                            "failure_type": error_type,
                            "failure_category": attempt_diagnostics["failure_category"],
                            "stage2_failure_category": attempt_diagnostics[
                                "failure_category"
                            ],
                        },
                        pipeline_context=True,
                    )
                    last_meta = {
                        **attempt_diagnostics,
                        "latency_ms": getattr(gateway_result, "latency_ms", None),
                        "model_name": attempt_diagnostics.get("model"),
                        "raw_text_length": len(last_raw_output),
                        "raw_response": _safe_response_summary(attempt_diagnostics),
                        "token_usage": getattr(gateway_result, "token_usage", None),
                        "response_format": payload.get("response_format"),
                        "validation_attempts": list(validation_attempts),
                        "candidate_sequences": split["candidate_sequences"],
                        "context_only_sequences": split["context_sequences"],
                        "pipeline_run_id": pipeline_run_id,
                        "assessment_batch_id": assessment_batch_id,
                        "batch_scheduler_attempt_count": (context or {}).get(
                            "batch_scheduler_attempt_count"
                        ),
                        "batch_scheduler_max_attempts": (context or {}).get(
                            "batch_scheduler_max_attempts"
                        ),
                        "external_call_count": external_call_count,
                        "repair_attempt_count": sum(
                            1
                            for item in validation_attempts
                            if item.get("attempt_type") == "repair"
                        ),
                        "gateway_retry_count": gateway_retry_count,
                        "compatibility_fallback_count": compatibility_fallback_count,
                    }
                if external_call_count >= max_attempts:
                    break
                if not gateway_result or (
                    not gateway_result.success
                    and error_type
                    not in {
                        "truncated_response",
                        "json_parse_error",
                        "invalid_response",
                        "reasoning_budget_exhausted",
                    }
                ):
                    break
        except Exception as exc:
            logger.exception(
                "LLMStateDetector.detect failed for group %s", (context or {}).get("group_id")
            )
            error_type = _exception_failure_type(exc)
            failure_category = normalize_stage2_failure(
                error_type,
                attempt_type=(
                    validation_attempts[-1].get("attempt_type")
                    if validation_attempts
                    else "initial"
                ),
                error_message=str(exc)[:200],
                exception=True,
            )
            _write_llm_audit(
                audit_context,
                None,
                failure=error_type,
                diagnostics={"failure_category": failure_category},
            )
            return LLMStateDetector._build_fallback_result(
                error_type,
                str(exc)[:200],
                attempt_count=len(validation_attempts) or 1,
                previous_meta=last_meta,
                failure_category=failure_category,
                pipeline_run_id=pipeline_run_id,
                assessment_batch_id=assessment_batch_id,
            )

        failure_category = (
            validation_attempts[-1].get("failure_category")
            if validation_attempts
            else normalize_stage2_failure(last_schema_error)
        )
        return LLMStateDetector._build_fallback_result(
            _failure_type(last_schema_error),
            str(last_error)[:200],
            schema_error=last_schema_error,
            attempt_count=len(validation_attempts) or 1,
            previous_meta=last_meta,
            failure_category=failure_category,
            pipeline_run_id=pipeline_run_id,
            assessment_batch_id=assessment_batch_id,
        )


__all__ = [
    "INTERVENTION_REASON_CODES",
    "LLMStateDetector",
    "RISK_SEGMENT_STATE_CODES",
    "SEGMENT_STATE_CODES",
    "SUB_CATEGORY_CANONICAL_COMPATIBILITY",
    "parse_llm_json_content",
    "replay_stage2_response",
]
