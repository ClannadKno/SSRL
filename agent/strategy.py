# -*- coding: utf-8 -*-
"""Strategy selection for Phase 10 intervention planning."""
import re

from knowledge_base import *
from auth import get_group_condition
from agent.detector import *


PHASE10_STRATEGY_VERSION = f"{SERA_STRATEGY_VERSION}|phase10_strategy_library_v1"

FORMAL_INTERVENTION_STATES = {
    "conflict_tension",
    "negative_silence",
    "blocked_frustration",
    "task_detached",
}

PASSIVE_OBSERVATION_STATES = {"positive_collaboration", "unknown"}


INTERVENTION_STRATEGY_LIBRARY = {
    "SILENCE_RESTART_DISCUSSION": {
        "template_ids": {
            "experiment": ["EXP-SIL-01", "EXP-SIL-04", "EXP-SIL-06"],
            "control": ["CTL-SIL-01", "CTL-SIL-04", "CTL-SIL-06"],
        },
        "fallback_template_key": "silence_no_text",
    },
    "SILENCE_INVITE_MULTIPLE_VIEWS": {
        "template_ids": {
            "experiment": ["EXP-LINT-01", "EXP-LINT-02", "EXP-LINT-04", "EXP-LINT-05"],
            "control": ["CTL-LINT-01", "CTL-LINT-02", "CTL-LINT-04", "CTL-LINT-05"],
        },
        "fallback_template_key": "silence_low_interaction",
    },
    "CONFLICT_FOCUS_ON_EVIDENCE": {
        "template_ids": {
            "experiment": ["EXP-CON-01", "EXP-CON-03", "EXP-CON-04"],
            "control": ["CTL-CON-01", "CTL-CON-03", "CTL-CON-05"],
        },
        "fallback_template_key": "conflict",
    },
    "CONFLICT_RESTATE_SHARED_GOAL": {
        "template_ids": {
            "experiment": ["EXP-CON-02", "EXP-CON-06"],
            "control": ["CTL-CON-02", "CTL-CON-04", "CTL-CON-06"],
        },
        "fallback_template_key": "conflict",
    },
    "FRUSTRATION_IDENTIFY_BLOCK": {
        "template_ids": {
            "experiment": ["EXP-FRU-02", "EXP-FRU-04"],
            "control": ["CTL-FRU-01", "CTL-FRU-02"],
        },
        "fallback_template_key": "frustration",
    },
    "FRUSTRATION_DECOMPOSE_PROBLEM": {
        "template_ids": {
            "experiment": ["EXP-FRU-01", "EXP-FRU-03", "EXP-FRU-06"],
            "control": ["CTL-FRU-03", "CTL-FRU-04", "CTL-FRU-06"],
        },
        "fallback_template_key": "frustration",
    },
    "FRUSTRATION_REVIEW_EXISTING_INFO": {
        "template_ids": {
            "experiment": ["EXP-FRU-05"],
            "control": ["CTL-FRU-05"],
        },
        "fallback_template_key": "frustration",
    },
    "OFFTASK_REFOCUS_GOAL": {
        "template_ids": {
            "experiment": ["EXP-OFF-01", "EXP-OFF-02", "EXP-OFF-06"],
            "control": ["CTL-OFF-01", "CTL-OFF-02", "CTL-OFF-06"],
        },
        "fallback_template_key": "offtask",
    },
    "OFFTASK_DEFINE_NEXT_ACTION": {
        "template_ids": {
            "experiment": ["EXP-OFF-03", "EXP-OFF-04", "EXP-OFF-05"],
            "control": ["CTL-OFF-03", "CTL-OFF-04", "CTL-OFF-05"],
        },
        "fallback_template_key": "offtask",
    },
    "PARTICIPATION_INVITE_QUIET_MEMBERS": {
        "template_ids": {
            "experiment": ["EXP-IMB-01", "EXP-IMB-03", "EXP-IMB-05"],
            "control": ["CTL-IMB-01", "CTL-IMB-03", "CTL-IMB-05"],
        },
        "fallback_template_key": "participation_imbalance",
    },
}


_TEMPLATE_ENTRY_BY_ID = {}
for _bank in (SERA_EXPERIMENT_TEMPLATES, SERA_CONTROL_TEMPLATES):
    for _entries in _bank.values():
        for _entry in _entries:
            _TEMPLATE_ENTRY_BY_ID[_entry["template_id"]] = _entry


def get_task_context_vars(context_summary=""):
    task = get_active_task()
    if task:
        task_topic = task["title"] or "本次项目式学习任务"
        task_goal = (task["description"] or "").strip()
        if len(task_goal) > 80:
            task_goal = task_goal[:80] + "..."
    else:
        task_topic = "本次项目式学习任务"
        task_goal = "形成一份小组协作成果"
    return {
        "task_topic": task_topic,
        "task_goal": task_goal or "形成一份小组协作成果",
        "output_format": "小组成果",
        "specific_constraint": extract_specific_constraint(context_summary),
        "smaller_scope": "一个更小、更具体、当前能完成的切入点",
        "remaining_time": "有限时间",
    }


def extract_specific_constraint(context_summary):
    if not context_summary:
        return "当前卡住的地方"
    for line in context_summary.splitlines():
        if any(w in line for w in ["不会", "难", "卡", "不懂", "没思路", "没意思", "无聊", "不想"]):
            clean = line.split("：", 1)[-1].strip()
            if clean:
                return clean[:40]
    return "当前卡住的地方"


def choose_member_name(context_rows):
    names = []
    for r in context_rows:
        try:
            name = r["real_name"]
        except Exception:
            name = None
        if name and name not in names:
            names.append(name)
    return names[0] if names else "这位同学"


def render_strategy_template(template, context_rows, context_summary):
    values = get_task_context_vars(context_summary)
    values["member_name"] = choose_member_name(context_rows)
    out = template or ""
    for k, v in values.items():
        out = out.replace("{" + k + "}", str(v))
    out = re.sub(r"{[^}]+}", "当前任务", out)
    return out


def _full_text(context_rows):
    return " ".join([(r["content"] or "") for r in context_rows])


def _state_tags(state):
    tags = state.get("evidence_tags") or state.get("tags") or []
    if isinstance(tags, str):
        tags = tags.replace("，", ",").replace("；", ",").replace(";", ",").split(",")
    return {str(tag).strip() for tag in tags if str(tag).strip()}


def _normalize_strategy_state(state):
    data = dict(state or {})
    normalized = normalize_state_payload(
        data.get("state_code") or data.get("fused_state_code") or "unknown",
        evidence_tags=data.get("evidence_tags") or data.get("tags"),
        assessment_status=data.get("assessment_status"),
    )
    data.update(
        {
            "state_code": normalized["state_code"],
            "state_label": data.get("state_label") or normalized["state_label"],
            "legacy_state_code": normalized["legacy_state_code"],
            "normalization_reason": normalized["normalization_reason"],
            "evidence_tags": normalized["evidence_tags"],
        }
    )
    return data


def infer_sub_category(state, context_rows):
    """将 detector 的粗粒度 state_code 转成更贴近话术库的子状态。"""
    state = _normalize_strategy_state(state)
    full_text = _full_text(context_rows)
    state_code = state.get("state_code")
    evidence_tags = _state_tags(state)
    msg_count = int(state.get("msg_count_10m", 0) or 0)
    unique_speakers = int(state.get("unique_speakers_10m", 0) or 0)
    speaker_ratio = float(state.get("speaker_ratio", 1) or 1)
    low_msg_count = int(state.get("low_msg_count", msg_count) or 0)
    low_unique_speakers = int(state.get("low_unique_speakers", unique_speakers) or 0)

    if state.get("recent_has_conflict") and not state.get("recent_conflict_repaired"):
        return "人际性冲突"
    if state.get("recent_has_conflict") and state.get("recent_conflict_repaired"):
        return "建设性冲突"

    if state.get("recent_has_offtask") and state.get("recent_has_return"):
        return "跑题脱离(有自调节)"
    if state.get("recent_has_offtask") and not state.get("recent_has_return"):
        return "跑题脱离(无自调节)"

    if state.get("online_no_text_silence"):
        return "在线沉默-无人发言"
    if state.get("online_low_interaction_silence"):
        if low_msg_count > 0 and low_unique_speakers <= 1:
            return "一人主导型沉默"
        return "在线沉默-低互动"

    constructive_hits = count_hits(full_text, CONSTRUCTIVE_CONFLICT_WORDS)
    destructive_hits = count_destructive_conflict_hits(full_text)
    if constructive_hits >= 1 and destructive_hits < 2 and state_code in {"conflict_tension", "positive_collaboration", "unknown"}:
        return "建设性冲突"
    if text_has_any(full_text, EXECUTION_WORDS) and state.get("avg_engagement", 3) >= 3.2 and state_code in {"unknown", "positive_collaboration"}:
        return "执行推进"
    if text_has_any(full_text, DEEP_THINKING_WORDS) and state.get("avg_engagement", 3) >= 3.2 and msg_count < 6:
        return "深度思考"

    coordination_hits = count_hits(full_text, COORDINATION_CONFUSION_WORDS)
    if state_code == "blocked_frustration" and ("coordination_blocked" in evidence_tags or "process_unclear" in evidence_tags):
        return "分工混乱"
    if coordination_hits >= 1:
        if text_has_any(full_text, ["谁来", "谁负责", "怎么分工", "没人记录", "没人整理", "没人总结"]):
            return "分工混乱"
        return "推进无序"

    if state_code == "conflict_tension":
        return "人际性冲突"
    if text_has_any(full_text, VALUE_DOUBT_WORDS):
        return "倦怠型"
    if state_code == "blocked_frustration":
        return "挫败型"
    if state_code == "negative_silence":
        if msg_count > 0 and unique_speakers <= 1 and speaker_ratio <= 0.5:
            return "一人主导型沉默"
        if msg_count <= 1:
            return "在线沉默-无人发言"
        return "在线沉默-低互动"
    if state_code == "task_detached":
        if text_has_any(full_text, PASSIVE_DETACHMENT_WORDS):
            return "敷衍脱离"
        return "跑题脱离(无自调节)"
    if state_code == "positive_collaboration":
        return "积极协作型"
    return "标准型"


def get_route(sub_category):
    return SERA_ROUTE_TABLE.get(sub_category) or SERA_ROUTE_TABLE["标准型"]


def get_template_key(sub_category):
    return get_route(sub_category).get("template_key", "default")


def choose_template_entry(group_id, condition, sub_category, context_summary):
    template_key = get_template_key(sub_category)
    bank = SERA_CONTROL_TEMPLATES if condition == "control" else SERA_EXPERIMENT_TEMPLATES
    entries = bank.get(template_key) or bank.get("default") or []
    if not entries:
        return tpl(
            "FALLBACK-01",
            "FALLBACK",
            "兜底提示",
            "普通情绪支持" if condition == "control" else "情绪-协作调节",
            "通用" if condition == "control" else "共同监控",
            "低",
            "请大家稳定一下状态，继续保持沟通。",
        )
    seed_text = f"{group_id}|{condition}|{sub_category}|{context_summary[-240:]}"
    idx = sum(ord(ch) for ch in seed_text) % len(entries)
    return entries[idx]


def clean_student_visible_message(message):
    """学生端不显示策略编号、SSRL标签或技术性标题。"""
    if not message:
        return "大家可以先把当前最卡住的地方说出来，我们再一起往下推进。"
    msg = message.strip()
    msg = re.sub(r"【?\[?SSRL\s*支持提示\]?】?", "", msg)
    msg = re.sub(r"\b(EA|EE|ER|SS|OI|EXP|CTL|CTRL|SSRL)-[A-Z0-9-]+\s*[^\n，：。]*", "", msg)
    msg = msg.replace("SSRL支持提示", "").replace("SSRL 支持提示", "")
    msg = re.sub(r"\n{3,}", "\n\n", msg).strip()
    return msg


def normalize_condition(condition):
    return "control" if condition == "control" else "experiment"


def _summarize_context_rows(context_rows):
    parts = []
    for row in (context_rows or [])[-8:]:
        content = (row.get("content") or "").strip() if isinstance(row, dict) else ""
        if not content:
            continue
        speaker = (row.get("real_name") or row.get("username") or row.get("role") or "成员") if isinstance(row, dict) else "成员"
        parts.append(f"{speaker}：{content[:80]}")
    return "\n".join(parts)


def _pick_entry(entries, seed_text):
    if not entries:
        return None
    idx = sum(ord(ch) for ch in (seed_text or "")) % len(entries)
    return entries[idx]


def _entry_by_template_ids(template_ids):
    entries = []
    for template_id in template_ids:
        entry = _TEMPLATE_ENTRY_BY_ID.get(template_id)
        if entry:
            entries.append(entry)
    return entries


def _choose_strategy_entry(group_id, condition, strategy_id, context_summary):
    strategy_meta = INTERVENTION_STRATEGY_LIBRARY[strategy_id]
    template_ids = strategy_meta["template_ids"][condition]
    entries = _entry_by_template_ids(template_ids)
    if entries:
        seed_text = f"{group_id}|{condition}|{strategy_id}|{context_summary[-240:]}"
        picked = _pick_entry(entries, seed_text)
        if picked:
            return picked
    bank = SERA_CONTROL_TEMPLATES if condition == "control" else SERA_EXPERIMENT_TEMPLATES
    fallback_entries = bank.get(strategy_meta["fallback_template_key"], [])
    if fallback_entries:
        return _pick_entry(fallback_entries, f"{group_id}|{condition}|{strategy_id}|fallback")
    return choose_template_entry(group_id, condition, "标准型", context_summary)


def _contains_any(text, tokens):
    return any(token in (text or "") for token in tokens)


def _select_strategy_id(state, sub_category, decision, context_summary):
    state_code = _normalize_strategy_state(state).get("state_code") or "unknown"
    if state_code not in FORMAL_INTERVENTION_STATES:
        return None
    strategy_category = (decision or {}).get("strategy_category") or ""
    text = context_summary or ""
    msg_count = int(state.get("msg_count_10m", 0) or 0)

    if state_code == "negative_silence":
        if "无人发言" in sub_category or msg_count <= 1:
            return "SILENCE_RESTART_DISCUSSION"
        if "参与" in sub_category or "主导" in sub_category:
            return "PARTICIPATION_INVITE_QUIET_MEMBERS"
        return "SILENCE_INVITE_MULTIPLE_VIEWS"

    if state_code == "conflict_tension":
        if _contains_any(text, ["目标", "任务", "要求", "主题", "题目"]):
            return "CONFLICT_RESTATE_SHARED_GOAL"
        return "CONFLICT_FOCUS_ON_EVIDENCE"

    if state_code == "blocked_frustration":
        if _contains_any(text, ["资料", "线索", "信息", "依据", "证据", "案例"]):
            return "FRUSTRATION_REVIEW_EXISTING_INFO"
        if _contains_any(text, ["不会", "卡住", "没思路", "不知道", "哪一步", "难"]):
            return "FRUSTRATION_IDENTIFY_BLOCK"
        return "FRUSTRATION_DECOMPOSE_PROBLEM"

    if state_code == "task_detached":
        if _contains_any(text, ["下一步", "先做", "分工", "行动", "推进"]) or strategy_category == "coordination_support":
            return "OFFTASK_DEFINE_NEXT_ACTION"
        return "OFFTASK_REFOCUS_GOAL"

    if strategy_category == "silence_restart":
        return "SILENCE_RESTART_DISCUSSION"
    if strategy_category == "participation_support":
        return "PARTICIPATION_INVITE_QUIET_MEMBERS"
    if strategy_category == "conflict_support":
        return "CONFLICT_FOCUS_ON_EVIDENCE"
    if strategy_category == "frustration_support":
        return "FRUSTRATION_IDENTIFY_BLOCK"
    if strategy_category == "task_refocus":
        return "OFFTASK_REFOCUS_GOAL"
    if strategy_category == "coordination_support":
        return "OFFTASK_DEFINE_NEXT_ACTION"
    return "SILENCE_INVITE_MULTIPLE_VIEWS"


def select_intervention_strategy(assessment, context, condition):
    state = _normalize_strategy_state(assessment or {})
    context_rows = list((context or {}).get("recent_student_messages") or (context or {}).get("context_rows") or [])
    context_summary = (context or {}).get("context_summary") or _summarize_context_rows(context_rows)
    llm_result = (context or {}).get("llm_result")
    group_id = int((context or {}).get("group_id") or state.get("group_id") or 0)
    condition = normalize_condition(condition)

    llm_sub_category = (llm_result or {}).get("sub_category")
    if llm_sub_category in SERA_ROUTE_TABLE:
        sub_category = llm_sub_category
    else:
        sub_category = state.get("sub_category") or infer_sub_category(state, context_rows)

    route = get_route(sub_category)
    route_should_intervene = bool(route.get("should_intervene", False))
    decision = (context or {}).get("decision") if isinstance((context or {}).get("decision"), dict) else None
    if isinstance(decision, dict) and "should_intervene" in decision:
        should_intervene = bool(decision.get("should_intervene"))
    else:
        should_intervene = route_should_intervene
    if decision is None and llm_result and llm_result.get("should_intervene") is False:
        should_intervene = False
    if state.get("state_code") not in FORMAL_INTERVENTION_STATES:
        should_intervene = False

    strategy_id = _select_strategy_id(state, sub_category, decision, context_summary)
    if strategy_id:
        entry = _choose_strategy_entry(group_id, condition, strategy_id, context_summary)
    else:
        entry = choose_template_entry(group_id, condition, "标准型", context_summary)
    raw_message = render_strategy_template(entry["text"], context_rows, context_summary)
    message = clean_student_visible_message(raw_message)
    title = "学习助手提示" if condition == "control" else entry["name"]
    is_oi_suppressed = 0 if should_intervene else 1

    return {
        "condition": condition,
        "sub_category": sub_category,
        "strategy_id": strategy_id,
        "intended_strategy_id": strategy_id,
        "template_id": entry["template_id"],
        "template_strategy_id": entry.get("strategy_id"),
        "strategy_name": entry["name"],
        "strategy_type": entry["type"],
        "ssrl_phase": entry["ssrl_phase"],
        "cognitive_load": entry["cognitive_load"],
        "strategy_category": decision.get("strategy_category") if isinstance(decision, dict) else sub_category,
        "strategy_version": PHASE10_STRATEGY_VERSION,
        "should_intervene": should_intervene,
        "is_oi_suppressed": is_oi_suppressed,
        "title": title,
        "message": message,
    }


def select_sera_strategy(group_id, state, context_rows, context_summary, llm_result=None, decision=None):
    """Backward-compatible wrapper over the standardized Phase 10 strategy selector."""
    context = {
        "group_id": group_id,
        "recent_student_messages": context_rows,
        "context_summary": context_summary,
        "llm_result": llm_result,
        "decision": decision,
    }
    return select_intervention_strategy(state, context, get_group_condition(group_id))
