# -*- coding: utf-8 -*-
"""Shared constants for the three-stage strategy pipeline schema.

The public-facing state ontology has four layers:

* primary sub-states: the 12 final states used by teacher/research surfaces;
* overlays: secondary tags that can coexist with a primary sub-state;
* process states: fallback/processing states, excluded from formal coverage;
* legacy states: coarse Stage 1 projection retained for compatibility/audit.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Optional

from services.three_stage_route_manifest import (
    REQUIRED_INTERVENTION,
    route_for_canonical_state,
)


THREE_STAGE_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
THREE_STAGE_SCHEMA_VERSION = "three_stage.v1"
STAGE2_SCHEMA_VERSION = "stage2.v1"
STAGE3_SCHEMA_VERSION = "stage3.v1"

COARSE_STATE_CODES = (
    "NO_RISK",
    "POSSIBLE_SILENCE",
    "POSSIBLE_CONFLICT",
    "POSSIBLE_BLOCKED",
    "POSSIBLE_DETACHMENT",
    "POSSIBLE_PARTICIPATION_PROBLEM",
    "POSSIBLE_POSITIVE",
    "UNKNOWN_COARSE",
    "EXPLICIT_HELP",
)

STAGE1_DECISIONS = (
    "NO_STRONG_INTERVENTION",
    "ESCALATE",
    "URGENT_ESCALATE",
)

PRIMARY_SUB_STATE_CODES = (
    "standard",
    "deep_thinking",
    "execution_progress",
    "constructive_conflict",
    "interpersonal_conflict",
    "confusion",
    "frustration",
    "burnout",
    "off_topic_self_regulated",
    "off_topic_unregulated",
    "perfunctory_detachment",
    "individual_marginalization",
)

FINAL_SUB_STATE_CODES = PRIMARY_SUB_STATE_CODES

STATE_OVERLAY_CODES = (
    "psychological_safety_risk",
    "high_intensity_overload",
    "stage_achievement",
)

PROCESS_STATE_CODES = (
    "unknown_sub_state",
    "unclassified",
    "observing",
)

LEGACY_STATE_CODES = (
    "positive_collaboration",
    "negative_silence",
    "conflict_tension",
    "blocked_frustration",
    "task_detached",
    "unknown",
)

# Backward-compatible Stage 2 vocabulary accepted from existing rows and older
# tests. Do not use this tuple to build final teacher/research distributions.
CANONICAL_SUB_STATE_CODES = (
    *PRIMARY_SUB_STATE_CODES,
    *STATE_OVERLAY_CODES,
    "unknown_sub_state",
)

STAGE2_ROUTABLE_STATE_CODES = CANONICAL_SUB_STATE_CODES
STAGE2_MODEL_OUTPUT_STATE_CODES = (*PRIMARY_SUB_STATE_CODES, "unknown_sub_state")

OVERLAY_COMPATIBLE_PRIMARY_STATES = {
    "psychological_safety_risk": ("interpersonal_conflict",),
    "high_intensity_overload": (
        "interpersonal_conflict",
        "standard",
        "frustration",
    ),
    "stage_achievement": ("execution_progress",),
}

OVERLAY_COMPATIBLE_PRIMARY_STATE = {
    tag: primary_states[0]
    for tag, primary_states in OVERLAY_COMPATIBLE_PRIMARY_STATES.items()
}

STATE_OVERLAY_SEMANTICS = {
    "psychological_safety_risk": {
        "compatible_primary_state": "interpersonal_conflict",
        "definition": "成员因嘲笑、贬低、打断、针对或人身攻击而不敢表达、发言或参与，安全表达已经受损。",
        "boundary": "仅有观点分歧、礼貌反对或已经完成的自发修复不足以标记；风险必须在该段仍有明确证据。",
    },
    "high_intensity_overload": {
        "compatible_primary_state": "frustration",
        "definition": "多项约束或信息同时压入，成员明确表示信息过多、注意力跟不上、反应不过来或持续高强度过载。",
        "boundary": "单个难点、普通忙碌或一次计算错误不足以标记；需要同时负荷或持续过载证据。",
    },
    "stage_achievement": {
        "compatible_primary_state": "execution_progress",
        "definition": "小组明确完成一个阶段目标、交付物或检查里程碑，并进入核对、收尾或下一阶段。",
        "boundary": "一般执行更新或尚未完成的分工不足以标记；需要明确的阶段完成或通过检查证据。",
    },
}

PROCESS_STATE_LABELS = {
    "unknown_sub_state": "子状态不确定",
    "unclassified": "未分类",
    "observing": "观察中",
}

LEGACY_STATE_LABELS = {
    "positive_collaboration": "积极协作",
    "negative_silence": "消极沉默",
    "conflict_tension": "紧张冲突",
    "blocked_frustration": "挫败卡住",
    "task_detached": "任务脱离",
    "unknown": "未知",
}

STATE_OVERLAY_LABELS = {
    "psychological_safety_risk": "心理安全风险",
    "high_intensity_overload": "高强度过载",
    "stage_achievement": "阶段达成",
}

CANONICAL_SUB_STATE_LABELS = {
    "standard": "常规协作",
    "deep_thinking": "深度思考",
    "execution_progress": "执行推进",
    "constructive_conflict": "建设性冲突",
    "interpersonal_conflict": "人际冲突",
    "confusion": "困惑",
    "frustration": "挫败",
    "burnout": "倦怠",
    "off_topic_self_regulated": "跑题后自主拉回",
    "off_topic_unregulated": "跑题未拉回",
    "perfunctory_detachment": "敷衍脱离",
    "individual_marginalization": "个体边缘化",
    **STATE_OVERLAY_LABELS,
    **PROCESS_STATE_LABELS,
}

# Compact, model-facing semantics for the single canonical ontology.  These
# definitions are intentionally separate from strategy routing: Stage 2 must
# first decide what is happening, then apply the deterministic route.
CANONICAL_SUB_STATE_SEMANTICS = {
    "standard": {
        "definition": "围绕任务的常规信息交换、补充、确认或初步共识，没有更鲜明的认知、情绪、冲突或脱离特征。",
        "boundary": "若出现明确的独立认知加工、固定分工执行、实质分歧或风险信号，应使用对应的更具体状态。",
    },
    "deep_thinking": {
        "definition": "成员明确提出独立核算、查阅、比较或反思，随后出现约30到120秒的任务对齐低互动，并给出有推理链的结果、修正或综合。",
        "boundary": "方案仍在探索或验证，尚未进入固定分工和交付执行；仅出现“比较、分析”等词或持续热烈讨论不足以判定。",
    },
    "execution_progress": {
        "definition": "方案或分工已基本确定，成员正在编写、计算、整理、提交、完成或用简短消息协调具体交付物。",
        "boundary": "不要把决策前的独立核算、资料查阅和方案权衡判为执行推进；若推进建立在反复忽视同一成员的任务相关建议、问题或参与请求之上，应优先判为个体边缘化。",
    },
    "constructive_conflict": {
        "definition": "成员对任务观点、证据或方案有实质分歧，但保持尊重，并通过共同标准、整合或自我调节走向收敛。",
        "boundary": "若核心是攻击、指责、讽刺、贬低或防御性对抗，应判为人际冲突。",
    },
    "interpersonal_conflict": {
        "definition": "分歧转向人际攻击、指责、讽刺、贬低、敌意或权力对抗，已损害安全表达和合作关系。",
        "boundary": "仅针对方案的理性反对且仍在整合，不属于人际冲突；若成员仍在相互攻击、指责或防御性对抗，即使其中一人说“算了、按你们的来”而退出争辩，主状态仍是人际冲突。",
    },
    "confusion": {
        "definition": "成员不理解要求、概念、信息关系或下一步路径，并明确求澄清或表现出认知不确定。",
        "boundary": "尚无反复失败和强烈负性情绪；若已多次受阻并表达烦躁或无能为力，应判为挫败。",
    },
    "frustration": {
        "definition": "成员因重复失败、障碍或无法推进而表达烦躁、受挫、无能为力，但仍在尝试完成任务。",
        "boundary": "若表现为持续耗竭、放弃或普遍失去投入，应判为倦怠。",
    },
    "burnout": {
        "definition": "成员对任务本身产生持续价值质疑或意义感流失，表达“做了也白做、没意思、不想继续”等广泛撤回投入；也可伴随高负荷后的疲惫、耗竭、放弃感。",
        "boundary": "目标通常清楚但成员认为不值得继续；一次具体障碍下仍在尝试更符合挫败，仅用空泛短句应付且没有持续价值质疑更符合敷衍脱离。",
    },
    "off_topic_self_regulated": {
        "definition": "小组曾偏离任务，但成员明确识别跑题并主动把讨论拉回任务，恢复已经发生。",
        "boundary": "若无明确拉回语言或后续仍持续跑题，应判为跑题未拉回。",
    },
    "off_topic_unregulated": {
        "definition": "讨论持续聚焦与任务无关的内容，且没有成员识别偏离或有效拉回。",
        "boundary": "已经明确自我纠偏并恢复任务对齐时，应判为跑题后自主拉回。",
    },
    "perfunctory_detachment": {
        "definition": "以敷衍、空泛、随便、机械附和或缺少实质内容的极短回复应付任务，显示低投入。",
        "boundary": "任务对齐的简短确认若服务于已确定的执行分工，属于执行推进；若明确质疑任务价值、认为认真投入也没有意义并普遍不想继续，应判为倦怠；当多名成员共同以“随便、差不多、能交就行”等最低限度内容应付时，即使其中一人偶尔提醒交付要求，只要没有稳定针对同一成员的排除关系，主状态仍是敷衍脱离。",
    },
    "individual_marginalization": {
        "definition": "有证据显示同一成员的任务相关建议、问题或参与请求被反复忽视、搁置、打断或排除，其他成员仍绕过该成员继续决策或执行，形成显著参与失衡。",
        "boundary": "仅某成员暂时未发言而没有排除或忽视证据不足以判定；必须区分中性任务贡献被反复绕过与冲突参与者的防御性退出，持续相互指责后仅说“算了、按你们的来”仍属于人际冲突；若同一成员已重复提出任务建议且末尾仍被明确绕过，则优先于执行推进；若多名成员都在轮流给出空泛、最低限度的应付内容，且没有稳定针对某一成员的排除关系，应判为敷衍脱离而非个体边缘化。",
    },
    "unknown_sub_state": {
        "definition": "候选消息证据不足、互相矛盾，或无法可靠区分任何正式主子状态。",
        "boundary": "不得为了覆盖率猜测；该值不计入正式主状态覆盖。",
    },
}


# Stage 2 uses these examples as a compact decision aid.  They describe
# state boundaries only; they are not scenario labels and must never be
# treated as evidence from the current candidate window.
STAGE2_STATE_BOUNDARY_GUIDANCE = {
    "confusion_vs_frustration": {
        "positive": "只是不理解要求、概念或下一步，并明确求澄清，尚未反复失败时判 confusion。",
        "counterexample": "同一障碍或方案反复失败，成员表达烦躁、受挫或无能为力但仍提供实质尝试时判 frustration。",
        "boundary": "一次失败、普通提问或信息不确定仍是 confusion；重复失败加负性受阻证据才切换为 frustration。",
        "recovered": "障碍已澄清、方案已恢复并在窗口末尾回到任务时，判 standard 或 execution_progress，不保留已恢复的风险状态。",
        "overlay": "若同时明确表示多项信息或约束过多，可添加 high_intensity_overload；overlay 不替换 primary state。",
    },
    "constructive_conflict_vs_interpersonal_conflict": {
        "positive": "围绕方案、证据或共同标准有实质分歧，仍能尊重彼此并整合结论时判 constructive_conflict。",
        "counterexample": "出现针对成员的攻击、归责、讽刺、贬低、敌意或防御性对抗时判 interpersonal_conflict。",
        "boundary": "“我不同意这个方案”属于观点分歧；“你怎么总是拖后腿”属于人际攻击，即使随后说“算了”也不改变主状态。",
        "recovered": "攻击或紧张已经明确停止，成员在窗口末尾恢复共同标准和任务协作时，判 standard 或 constructive_conflict。",
        "overlay": "interpersonal_conflict 可在仍有安全表达受损证据时添加 psychological_safety_risk；普通观点分歧不能添加该 overlay。",
    },
    "frustration_vs_perfunctory_detachment": {
        "positive": "成员反复尝试、提供具体任务内容并因障碍表达受挫时判 frustration，不因语气泄气而判 perfunctory_detachment。",
        "counterexample": "成员用“随便、差不多、能交就行”等空泛最低限度内容应付，缺少具体尝试和投入时判 perfunctory_detachment。",
        "boundary": "持续尝试且有实质内容不等于敷衍；低投入、机械附和或普遍放弃实质参与才是 perfunctory_detachment。",
        "recovered": "障碍解决后重新给出具体方案、分工或交付内容时，窗口末尾判 standard 或 execution_progress。",
        "overlay": "frustration 可叠加 high_intensity_overload，但强度或信息量本身不能把 frustration 改判为 interpersonal_conflict。",
    },
    "perfunctory_detachment_vs_individual_marginalization": {
        "positive": "多人轮流用空泛最低限度内容应付，且没有稳定针对某一成员的排除关系时判 perfunctory_detachment。",
        "counterexample": "同一成员多次提出中性任务建议、问题或参与请求，却被反复忽视、搁置或绕过，其他人仍继续决策时判 individual_marginalization。",
        "boundary": "某人偶尔说“算了、按你们的来”不足以构成个体边缘化；需要稳定、定向、可追溯到同一成员的排除证据。",
        "recovered": "被忽视的成员重新被回应并参与后续任务，窗口末尾已恢复共同推进时，判 standard 或 execution_progress。",
        "overlay": "多人共同低投入不能仅因一人提醒交付就加 individual_marginalization；明确的 overload 仍只能作为 compatible primary state 的 overlay。",
    },
    "standard_vs_execution_progress": {
        "positive": "围绕任务交换信息但尚未形成固定分工、交付物或执行动作时判 standard。",
        "counterexample": "方案或分工已经确定，成员正在编写、计算、整理、提交或协调具体交付物时判 execution_progress。",
        "boundary": "“我们可以比较这两个方案”仍是普通讨论；“你负责表格，我现在录入数据”是执行推进。简短不等于敷衍。",
        "recovered": "风险或冲突结束后重新回到明确分工和交付，判 execution_progress；只恢复一般交流则判 standard。",
        "overlay": "execution_progress 可叠加 stage_achievement；standard、frustration 或 interpersonal_conflict 也可按证据叠加 high_intensity_overload，overlay 不改变 primary。",
    },
    "primary_state_vs_high_intensity_overload": {
        "positive": "先根据任务、认知和关系证据确定一个 primary state，再把同时存在的信息或约束过载标为 high_intensity_overload。",
        "counterexample": "仅因为消息密集、任务复杂或表达强烈就判 interpersonal_conflict，属于错误；没有人际攻击时不得这样改判。",
        "boundary": "high_intensity_overload 是 secondary overlay，不是独立 primary；输出时 sub_category 可为 high_intensity_overload，但 canonical_state 必须明确填写兼容的 primary state。",
        "recovered": "过载已解除且窗口末尾回到任务推进时，保留 execution_progress 或 standard 等当前 primary，不把已恢复的过载当作当前风险。",
        "overlay": "只有明确的信息/约束同时过载证据才添加 high_intensity_overload；普通忙碌、单个难点或一次计算错误不足以添加。",
    },
}

FINAL_SUB_STATE_LABELS = {
    code: CANONICAL_SUB_STATE_LABELS[code] for code in FINAL_SUB_STATE_CODES
}

STATE_ONTOLOGY = {
    "primary_sub_states": tuple(PRIMARY_SUB_STATE_CODES),
    "state_overlays": tuple(STATE_OVERLAY_CODES),
    "process_states": tuple(PROCESS_STATE_CODES),
    "legacy_states": tuple(LEGACY_STATE_CODES),
    "labels": {
        **FINAL_SUB_STATE_LABELS,
        **STATE_OVERLAY_LABELS,
        **PROCESS_STATE_LABELS,
        **LEGACY_STATE_LABELS,
    },
    "semantics": CANONICAL_SUB_STATE_SEMANTICS,
}

OI_STRATEGY_IDS = ("OI-001", "OI-002", "OI-003", "OI-004")

def _route_payload_for_schema(canonical_sub_state: str) -> dict[str, Any]:
    route = route_for_canonical_state(canonical_sub_state)
    candidate_strategy_ids = (
        *route["primary_strategy_ids"],
        *route["backup_strategy_ids"],
    )
    return {
        "candidate_strategy_ids": tuple(dict.fromkeys(candidate_strategy_ids)),
        "primary_strategy_ids": tuple(route["primary_strategy_ids"]),
        "backup_strategy_ids": tuple(route["backup_strategy_ids"]),
        "route_mode": route["route_mode"],
        "intervention_mode": route["intervention_mode"],
        "should_intervene": route["route_mode"] == REQUIRED_INTERVENTION,
        "inhibition_strategy_id": route["inhibition_strategy_id"],
    }


SUB_STATE_STRATEGY_ROUTES = {
    code: _route_payload_for_schema(code) for code in STAGE2_ROUTABLE_STATE_CODES
}

INTERVENTION_SUB_STATE_CODES = tuple(
    code
    for code in PRIMARY_SUB_STATE_CODES
    if SUB_STATE_STRATEGY_ROUTES[code]["should_intervene"]
)

NON_INTERVENTION_SUB_STATE_CODES = tuple(
    code
    for code in PRIMARY_SUB_STATE_CODES
    if not SUB_STATE_STRATEGY_ROUTES[code]["should_intervene"]
)

CANONICAL_TO_LEGACY_STATE_CODE = {
    "standard": "positive_collaboration",
    "deep_thinking": "positive_collaboration",
    "execution_progress": "positive_collaboration",
    "constructive_conflict": "positive_collaboration",
    "interpersonal_conflict": "conflict_tension",
    "confusion": "blocked_frustration",
    "frustration": "blocked_frustration",
    "burnout": "blocked_frustration",
    "off_topic_self_regulated": "task_detached",
    "off_topic_unregulated": "task_detached",
    "perfunctory_detachment": "task_detached",
    "individual_marginalization": "task_detached",
    "psychological_safety_risk": "conflict_tension",
    "high_intensity_overload": "blocked_frustration",
    "stage_achievement": "positive_collaboration",
    "unknown_sub_state": "unknown",
}


def dumps_json(value: Any) -> str:
    """Serialize JSON fields consistently for three-stage audit rows."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def normalize_canonical_sub_state(value: Optional[str]) -> str:
    """Return a known Stage 2 route state or the explicit unknown process value.

    This is a legacy-compatible normalizer for persisted Stage 2 data. Use
    :func:`normalize_final_sub_state` when building public final-state fields.
    """

    normalized = (value or "").strip()
    if normalized in STAGE2_ROUTABLE_STATE_CODES:
        return normalized
    return "unknown_sub_state"


def normalize_final_sub_state(value: Optional[str]) -> Optional[str]:
    """Return a public final primary sub-state, or ``None`` when not final."""

    normalized = (value or "").strip()
    if normalized in FINAL_SUB_STATE_CODES:
        return normalized
    if normalized in STATE_OVERLAY_CODES:
        return OVERLAY_COMPATIBLE_PRIMARY_STATE[normalized]
    return None


def legacy_state_for_sub_state(value: Optional[str]) -> str:
    """Project a Stage 2 state to the coarse legacy audit state code."""

    return CANONICAL_TO_LEGACY_STATE_CODE[normalize_canonical_sub_state(value)]


def is_primary_sub_state(value: Optional[str]) -> bool:
    return (value or "").strip() in PRIMARY_SUB_STATE_CODES


def is_state_overlay(value: Optional[str]) -> bool:
    return (value or "").strip() in STATE_OVERLAY_CODES


def is_process_state(value: Optional[str]) -> bool:
    return (value or "").strip() in PROCESS_STATE_CODES


def is_legacy_state(value: Optional[str]) -> bool:
    return (value or "").strip() in LEGACY_STATE_CODES


def label_for_state_code(value: Optional[str]) -> str:
    code = (value or "").strip()
    return STATE_ONTOLOGY["labels"].get(code, code or "")


def _loads_json_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except (TypeError, ValueError):
            return []
        return loaded if isinstance(loaded, list) else []
    return []


def _state_overlays_from(*values: Any) -> list[str]:
    overlays = []
    for value in values:
        for item in _loads_json_list(value):
            code = str(item or "").strip()
            if is_state_overlay(code) and code not in overlays:
                overlays.append(code)
        code = str(value or "").strip() if not isinstance(value, (list, tuple)) else ""
        if is_state_overlay(code) and code not in overlays:
            overlays.append(code)
    return overlays


@dataclass(frozen=True)
class FinalMessageStateResolver:
    """Build the canonical public state contract for one segment/message row."""

    @staticmethod
    def resolve(
        *,
        canonical_sub_state_code: Optional[str] = None,
        raw_sub_state_code: Optional[str] = None,
        secondary_tags_json: Any = None,
        secondary_sub_state_tags_json: Any = None,
        coarse_state_code: Optional[str] = None,
        state_code: Optional[str] = None,
        assessment_status: Optional[str] = None,
        assignment_source: Optional[str] = None,
        inferred: bool = False,
        confidence: Optional[float] = None,
        segment_id: Optional[int] = None,
        assessment_batch_id: Optional[int] = None,
        strategy_pipeline_run_id: Optional[int] = None,
        selected_strategy_id: Optional[str] = None,
        inhibition_strategy_id: Optional[str] = None,
    ) -> dict[str, Any]:
        raw_code = str(
            canonical_sub_state_code or raw_sub_state_code or ""
        ).strip()
        overlays = _state_overlays_from(
            secondary_tags_json,
            secondary_sub_state_tags_json,
        )
        inferred_value = bool(inferred)
        final_code = normalize_final_sub_state(raw_code)
        if is_state_overlay(raw_code):
            if raw_code not in overlays:
                overlays.insert(0, raw_code)
            inferred_value = True
            assignment_source = assignment_source or "legacy_overlay_projection"
        elif is_process_state(raw_code):
            assignment_source = assignment_source or raw_code
        elif not final_code and is_legacy_state(state_code):
            assignment_source = assignment_source or "legacy_coarse_only"

        coarse = (
            str(coarse_state_code or "").strip()
            or (
                legacy_state_for_sub_state(raw_code)
                if raw_code in STAGE2_ROUTABLE_STATE_CODES
                else ""
            )
            or (str(state_code or "").strip() if is_legacy_state(state_code) else "")
        )
        legacy = (
            coarse
            if is_legacy_state(coarse)
            else (
                legacy_state_for_sub_state(final_code)
                if final_code
                else (str(state_code or "").strip() if is_legacy_state(state_code) else "")
            )
        )
        status = str(assessment_status or "").strip() or (
            "confirmed" if final_code else "unclassified"
        )
        return {
            "final_sub_state_code": final_code,
            "final_sub_state_label": (
                FINAL_SUB_STATE_LABELS.get(final_code) if final_code else None
            ),
            "coarse_state_code": coarse or None,
            "legacy_state_code": legacy or None,
            "assessment_status": status,
            "assignment_source": assignment_source or "model_segment",
            "inferred": inferred_value,
            "confidence": confidence,
            "segment_id": segment_id,
            "assessment_batch_id": assessment_batch_id,
            "strategy_pipeline_run_id": strategy_pipeline_run_id,
            "selected_strategy_id": selected_strategy_id,
            "inhibition_strategy_id": inhibition_strategy_id,
            "state_overlays": overlays,
        }


def route_for_sub_state(value: Optional[str]) -> dict[str, Any]:
    """Return a mutable routing payload for a canonical Stage 2 sub-state."""

    from services.three_stage_strategy_library import route_for_sub_state as _route_from_library

    return _route_from_library(normalize_canonical_sub_state(value))


def route_for_state_with_overlays(
    value: Optional[str],
    secondary_tags: Any = None,
) -> dict[str, Any]:
    """Return the effective route after applying explicit overlay priority."""

    canonical = normalize_canonical_sub_state(value)
    tags = _state_overlays_from(secondary_tags)
    for overlay in (
        "psychological_safety_risk",
        "high_intensity_overload",
        "stage_achievement",
    ):
        if overlay not in tags:
            continue
        if canonical not in OVERLAY_COMPATIBLE_PRIMARY_STATES.get(overlay, ()):
            continue
        route = route_for_sub_state(overlay)
        route["canonical_sub_state"] = canonical
        route["route_overlay_tag"] = overlay
        return route
    route = route_for_sub_state(canonical)
    route["route_overlay_tag"] = None
    return route
