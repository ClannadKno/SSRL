# -*- coding: utf-8 -*-
"""结构化策略库服务。

策略库改造为结构化数据：
- id
- version
- applicable_states
- goal
- generator_instruction
- max_chars
- cooldown_seconds
- fallback_template
"""
from typing import Optional

from db import query_one, query_all, execute


FINAL_STATE_CODES = {
    "positive_collaboration",
    "conflict_tension",
    "negative_silence",
    "blocked_frustration",
    "task_detached",
    "unknown",
}

FORMAL_INTERVENTION_STATES = {
    "conflict_tension",
    "negative_silence",
    "blocked_frustration",
    "task_detached",
}

PASSIVE_OBSERVATION_STATES = {"positive_collaboration", "unknown"}


# 内置策略定义（V2 结构化策略库）
BUILTIN_STRATEGIES = [
    {
        "id": "v2_silence_restart",
        "version": "v2.3",
        "applicable_states": ["negative_silence"],
        "goal": "打破沉默，鼓励开始讨论",
        "generator_instruction": "请用1句中文帮助小组重新开口，只给一个低门槛表达动作，不催促、不安慰泛化。",
        "max_chars": 90,
        "cooldown_seconds": 120,
        "strategy_type": "active_intervention",
        "sub_category": "silence",
        "display_name": "打破沉默-重启讨论",
        "fallback_template": "可以先不用完整回答，每人发一句当前想法或疑问，把讨论重新启动起来。",
        "fallback_template_control": "讨论暂时慢下来是正常的，大家可以放松一下，继续保持信心。",
    },
    {
        "id": "v2_silence_invite",
        "version": "v2.3",
        "applicable_states": ["negative_silence"],
        "goal": "邀请所有成员参与讨论",
        "generator_instruction": "请用1句中文邀请小组成员各补一个短输入，不点名、不公开评价参与多少。",
        "max_chars": 90,
        "cooldown_seconds": 120,
        "strategy_type": "active_intervention",
        "sub_category": "participation",
        "display_name": "邀请全员参与",
        "fallback_template": "可以让还没充分表达的同学各补充一个问题、证据或建议。",
        "fallback_template_control": "每个人参与节奏不同是正常的，大家都可以继续贡献想法。",
    },
    {
        "id": "v2_conflict_evidence",
        "version": "v2.3",
        "applicable_states": ["conflict_tension"],
        "goal": "引导冲突双方关注证据而非人身",
        "generator_instruction": "请用1句中文把讨论从对人转向依据比较，不裁判谁对谁错。",
        "max_chars": 90,
        "cooldown_seconds": 120,
        "strategy_type": "active_intervention",
        "sub_category": "conflict",
        "display_name": "引导关注证据",
        "fallback_template": "先不判断谁对谁错，请把不同观点各写出一个依据，再看哪些可以合并。",
        "fallback_template_control": "出现不同意见是正常的，请大家保持耐心和尊重。",
    },
    {
        "id": "v2_conflict_goal",
        "version": "v2.3",
        "applicable_states": ["conflict_tension"],
        "goal": "重申共同目标，化解冲突",
        "generator_instruction": "请用1句中文把小组拉回共同任务目标，再给一个依据对齐动作。",
        "max_chars": 90,
        "cooldown_seconds": 120,
        "strategy_type": "active_intervention",
        "sub_category": "conflict",
        "display_name": "重申共同目标",
        "fallback_template": "先不判断谁对谁错，请把不同观点各写出一个依据，再看哪些可以合并。",
        "fallback_template_control": "出现不同意见是正常的，请大家保持耐心和尊重。",
    },
    {
        "id": "v2_frustration_identify",
        "version": "v2.3",
        "applicable_states": ["blocked_frustration"],
        "goal": "帮助小组识别卡住的具体问题",
        "generator_instruction": "请用1句中文把笼统卡住缩小为一个可说清的卡点，不给任务答案。",
        "max_chars": 90,
        "cooldown_seconds": 120,
        "strategy_type": "active_intervention",
        "sub_category": "stuck",
        "display_name": "识别卡点",
        "fallback_template": "先别急着解决全部问题，请只写出当前最卡的一点，再讨论下一步。",
        "fallback_template_control": "遇到卡点很正常，不代表你们做得不好，保持信心继续尝试。",
    },
    {
        "id": "v2_frustration_decompose",
        "version": "v2.3",
        "applicable_states": ["blocked_frustration"],
        "goal": "帮助分解复杂问题",
        "generator_instruction": "请用1句中文让小组先拆出下一步最小动作，不替他们完成分析。",
        "max_chars": 90,
        "cooldown_seconds": 120,
        "strategy_type": "active_intervention",
        "sub_category": "stuck",
        "display_name": "分解复杂问题",
        "fallback_template": "先别急着解决全部问题，请只写出当前最卡的一点，再讨论下一步。",
        "fallback_template_control": "遇到卡点很正常，不代表你们做得不好，保持信心继续尝试。",
    },
    {
        "id": "v2_offtask_refocus",
        "version": "v2.3",
        "applicable_states": ["task_detached"],
        "goal": "将讨论拉回正题",
        "generator_instruction": "请用1句中文轻量拉回当前任务成果要求，并给一个短时目标。",
        "max_chars": 90,
        "cooldown_seconds": 120,
        "strategy_type": "active_intervention",
        "sub_category": "off_task",
        "display_name": "拉回正题",
        "fallback_template": "先把话题拉回任务：确认现在要完成什么，再定一个马上能做的小步骤。",
        "fallback_template_control": "讨论中偶尔分散注意力很正常，大家可以调整状态后继续。",
    },
    {
        "id": "v2_offtask_next_action",
        "version": "v2.3",
        "applicable_states": ["task_detached"],
        "goal": "帮助小组明确下一步行动",
        "generator_instruction": "请用1句中文把偏离讨论转成一个马上能做的任务动作。",
        "max_chars": 90,
        "cooldown_seconds": 120,
        "strategy_type": "active_intervention",
        "sub_category": "off_task",
        "display_name": "明确下一步",
        "fallback_template": "先把话题拉回任务：确认现在要完成什么，再定一个马上能做的小步骤。",
        "fallback_template_control": "讨论中偶尔分散注意力很正常，大家可以调整状态后继续。",
    },
    {
        "id": "v2_participation_encourage",
        "version": "v2.3",
        "applicable_states": ["negative_silence"],
        "goal": "在低互动沉默中邀请更多成员补充",
        "generator_instruction": "请用1句中文让小组各补一个问题、证据或建议，不点名、不评价个人表现。",
        "max_chars": 90,
        "cooldown_seconds": 120,
        "strategy_type": "active_intervention",
        "sub_category": "silence_participation",
        "display_name": "沉默-邀请补充",
        "fallback_template": "可以让还没充分表达的同学各补充一个问题、证据或建议。",
        "fallback_template_control": "每个人参与节奏不同是正常的，大家都可以继续贡献想法。",
    },
    {
        "id": "v2_coordination_support",
        "version": "v2.3",
        "applicable_states": ["blocked_frustration"],
        "goal": "帮助小组从协调卡住转向明确分工",
        "generator_instruction": "请用1句中文让小组先确认一个记录者或下一步责任，不替他们分配完整方案。",
        "max_chars": 90,
        "cooldown_seconds": 120,
        "strategy_type": "active_intervention",
        "sub_category": "coordination_blocked",
        "display_name": "卡住-协调分工",
        "fallback_template": "先确认一个记录者，再决定接下来最先完成的一件事。",
        "fallback_template_control": "协作中出现一点混乱很正常，大家保持耐心，继续协调。",
    },
    {
        "id": "v2_general_support",
        "version": "v2.3",
        "applicable_states": [],
        "goal": "一般性学习支持",
        "generator_instruction": "仅在学生主动求助或人工确认时使用；自动介入默认不使用该策略。",
        "max_chars": 90,
        "cooldown_seconds": 300,
        "strategy_type": "ordinary_agent_support",
        "sub_category": "unknown",
        "display_name": "一般性支持",
        "fallback_template": "可以先停一下，确认当前目标和下一步要做的一件事。",
        "fallback_template_control": "大家可以保持现在的节奏，继续稳定推进讨论。",
    },
]


class StrategyService:
    """V2 结构化策略库服务。"""

    @staticmethod
    def get_strategy(strategy_id: str) -> Optional[dict]:
        """按 ID 获取策略定义。"""
        for s in BUILTIN_STRATEGIES:
            if s["id"] == strategy_id:
                return dict(s)
        return None

    @staticmethod
    def find_strategies_for_state(state_code: str, max_results: int = None) -> list:
        """查找适用于指定状态的正式介入策略。

        `positive_collaboration` 与 `unknown` 默认只观察，不返回自动介入候选。
        旧状态只能作为 evidence tag 使用，不能直接映射成正式介入状态。
        """
        if max_results is None:
            max_results = 3
        if state_code not in FORMAL_INTERVENTION_STATES:
            return []
        candidates = [
            s for s in BUILTIN_STRATEGIES
            if state_code in s["applicable_states"]
        ]
        candidates.sort(key=lambda s: (s["cooldown_seconds"], s["id"]))
        return candidates[:max_results]

    @staticmethod
    def find_strategies_for_group(group_id: int, state_code: str, max_results: int = None) -> list:
        """查找适用于指定房间和状态的策略列表。"""
        return StrategyService.find_strategies_for_state(state_code, max_results)

    @staticmethod
    def get_all_strategies() -> list:
        """获取所有策略定义。"""
        return list(BUILTIN_STRATEGIES)

    @staticmethod
    def validate_all_strategies() -> list:
        """校验所有内置策略字段完整性，返回缺失字段列表。"""
        required_fields = {"id", "version", "strategy_type", "sub_category", "display_name",
                          "applicable_states", "goal", "generator_instruction",
                          "max_chars", "cooldown_seconds", "fallback_template"}
        issues = []
        for s in BUILTIN_STRATEGIES:
            missing = required_fields - set(s.keys())
            if missing:
                issues.append({"id": s.get("id", "unknown"), "missing_fields": sorted(missing)})
            if s.get("strategy_type") == "active_intervention":
                invalid_states = [
                    code for code in s.get("applicable_states", [])
                    if code not in FORMAL_INTERVENTION_STATES
                ]
                if invalid_states:
                    issues.append({"id": s.get("id", "unknown"), "invalid_applicable_states": invalid_states})
                if int(s.get("cooldown_seconds") or 0) != 120:
                    issues.append({"id": s.get("id", "unknown"), "invalid_cooldown_seconds": s.get("cooldown_seconds")})
        return issues

    @staticmethod
    def build_strategy_prompt(strategy: dict, context: dict) -> str:
        """根据策略定义和上下文构建 LLM 提示。"""
        parts = [
            f"## 策略目标\n{strategy['goal']}",
            f"## 生成指令\n{strategy['generator_instruction']}",
            f"## 约束\n- 回复长度不超过 {strategy['max_chars']} 字\n- 不要直接给出完整任务答案\n- 不要公开批评学生\n- 不要捏造不存在的讨论内容",
        ]
        return "\n\n".join(parts)

    @staticmethod
    def check_strategy_cooldown(group_id: int, strategy_id: str) -> bool:
        """检查特定策略是否在冷却期内。返回 True 表示可以介入。"""
        strategy = StrategyService.get_strategy(strategy_id)
        if not strategy:
            return True
        cooldown = strategy.get("cooldown_seconds", 60)
        from datetime import datetime, timedelta
        since = (datetime.now() - timedelta(seconds=cooldown)).strftime("%Y-%m-%d %H:%M:%S")
        row = query_one(
            """SELECT COUNT(*) AS c FROM intervention_runs
               WHERE group_id=? AND strategy_id=? AND created_at>=? AND status IN ('PUBLISHED','FALLBACK')""",
            (group_id, strategy_id, since),
        )
        count = int(row["c"]) if row else 0
        return count == 0
