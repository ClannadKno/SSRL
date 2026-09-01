# -*- coding: utf-8 -*-
"""
SSRL 领域知识库：
1. 小组状态元数据；
2. 规则识别所需关键词；
3. 实验组“SSRL智能情绪—协作调节”话术库；
4. 对照组“普通情绪支持”话术库；
5. 子状态到话术类别的路由表。

研究边界：
- 实验组允许提供“过程性引导”：轮流表达、观点澄清、任务拆解、角色分工、目标重聚焦、进度监控、总结反思。
- 实验组不提供任务答案、不替学生完成项目内容。
- 对照组只提供一般性理解、安慰、肯定和鼓励，不提供明确的共享调节步骤。
"""

# -----------------------------
# 情绪与文本规则引擎
# -----------------------------
STATE_META = {
    "positive_collaboration": ("积极协作", 1, "低风险"),
    "negative_silence": ("消极沉默", 3, "高风险"),
    "conflict_tension": ("紧张冲突", 3, "高风险"),
    "blocked_frustration": ("挫败卡住", 2, "中风险"),
    "task_detached": ("任务脱离", 2, "中风险"),
    "unknown": ("观察中", 1, "低风险"),
}

FINAL_STATE_CODES = tuple(STATE_META.keys())

LEGACY_PRIMARY_STATE_CODES = frozenset({
    "participation_imbalance",
    "coordination_disorder",
    "conflict_repair",
    "positive_recovery",
    "insufficient_evidence",
})

LEGACY_STATE_LABELS = {
    "participation_imbalance": "参与不均",
    "coordination_disorder": "协作推进无序",
    "conflict_repair": "冲突修复",
    "positive_recovery": "积极恢复",
    "insufficient_evidence": "证据不足",
}

_CONFLICT_TAGS = {
    "direct_disagreement",
    "mutual_rebuttal",
    "personalized_blame",
    "dominance_conflict",
    "unrepaired_conflict",
}

_DETACHED_TAGS = {
    "off_task_topic",
    "passive_withdrawal",
    "low_motivation",
}

_RECOVERY_TAGS = {
    "return_to_task",
    "role_redistribution",
    "multi_member_followup",
    "summary_progress",
    "risk_resolved",
    "positive_recovery",
}


def _coerce_evidence_tags(*values):
    tags = []
    for value in values:
        if not value:
            continue
        if isinstance(value, dict):
            iterable = list(value.keys()) + list(value.values())
        elif isinstance(value, (list, tuple, set)):
            iterable = value
        else:
            iterable = str(value).replace("，", ",").replace("；", ",").replace(";", ",").split(",")
        for item in iterable:
            text = str(item or "").strip()
            if text and text not in tags:
                tags.append(text)
    return tags


def normalize_state_payload(state_code, *, evidence_tags=None, assessment_status=None):
    """Normalize historical/legacy state codes into the 6 final primary states."""
    raw_code = str(state_code or "").strip()
    code = raw_code.lower()
    tags = _coerce_evidence_tags(evidence_tags)
    legacy_state_code = code if code in LEGACY_PRIMARY_STATE_CODES else None
    normalization_reason = None

    if not code:
        code = "unknown"
        normalization_reason = "empty_state_code_normalized_to_unknown"
    elif code in STATE_META:
        pass
    elif code == "participation_imbalance":
        tag_set = set(tags)
        if tag_set & _CONFLICT_TAGS:
            code = "conflict_tension"
        elif tag_set & _DETACHED_TAGS:
            code = "task_detached"
        else:
            code = "unknown"
        normalization_reason = "legacy_participation_imbalance_normalized"
    elif code == "coordination_disorder":
        code = "blocked_frustration"
        normalization_reason = "legacy_coordination_disorder_normalized"
    elif code == "conflict_repair":
        code = "positive_collaboration" if set(tags) & _RECOVERY_TAGS else "conflict_tension"
        normalization_reason = "legacy_conflict_repair_normalized"
    elif code == "positive_recovery":
        code = "positive_collaboration"
        normalization_reason = "legacy_positive_recovery_normalized"
    elif code == "insufficient_evidence":
        code = "unknown"
        normalization_reason = "legacy_insufficient_evidence_normalized_to_unknown"
    else:
        legacy_state_code = raw_code or None
        code = "unknown"
        normalization_reason = "unknown_state_code_normalized_to_unknown"

    if legacy_state_code and legacy_state_code not in tags:
        tags.append(legacy_state_code)
    if assessment_status == "insufficient_evidence" and code not in STATE_META:
        code = "unknown"
        normalization_reason = normalization_reason or "insufficient_evidence_normalized_to_unknown"

    state_label, risk_level, risk_label = STATE_META.get(code, STATE_META["unknown"])
    return {
        "state_code": code,
        "state_label": state_label,
        "risk_level": risk_level,
        "risk_label": risk_label,
        "legacy_state_code": legacy_state_code,
        "normalization_reason": normalization_reason,
        "evidence_tags": tags,
    }


FRUSTRATION_WORDS = [
    "不会", "太难", "难死", "算了", "做不下去", "不懂", "没思路", "卡住", "烦",
    "崩溃", "不知道", "麻烦", "不想做", "没办法", "做不出来", "没头绪",
]
CONFLICT_WORDS = [
    "不对", "你错", "不是这样", "不同意", "反对", "凭什么", "不合理", "别说了",
    "我觉得你", "你这样", "不行", "争", "吵",
    "没搞懂", "你没搞懂", "强行扯", "强行", "听我的", "按你的来", "按我的来",
    "你们俩", "没道理", "才没道理", "想法不行", "你这个想法不行",
    "你又不是", "又不是组长", "凭什么听你的", "谁听你的", "别管", "闭嘴",
    "你懂什么", "乱说", "别乱说", "你不懂", "你别说", "别插嘴",
]
OFF_TASK_WORDS = [
    "吃饭", "中午", "午饭", "晚饭", "午餐", "晚餐", "吃什么", "吃啥", "吃点", "去哪吃", "饿了", "饭",
    "食堂", "外卖", "点外卖", "奶茶", "零食", "麻辣烫", "火锅", "咖啡", "肯德基", "麦当劳", "汉堡", "披萨",
    "综艺", "电视剧", "电影", "追剧", "看剧", "演唱会", "明星", "爱豆", "偶像", "好笑", "搞笑", "笑死",
    "视频", "看视频", "刷视频", "短视频", "抖音", "b站", "直播", "番剧", "动漫", "小说", "八卦", "瓜",
    "游戏", "打游戏", "开黑", "上分", "段位", "皮肤", "氪金", "手游", "玩", "周末", "出去玩", "逛街",
    "睡觉", "困了", "无聊死了", "聊天", "下课", "放学", "调课", "假期", "旅游",
]
LOW_MOTIVATION_WORDS = [
    "无聊", "没意思", "随便", "随便吧", "都行", "反正", "听你们的", "不想做",
    "不做", "懒得", "没什么想说", "没啥想说", "我没想法", "我不知道说什么",
    "无所谓", "没兴趣", "不感兴趣", "不想讨论", "烦死",
]
POSITIVE_WORDS = [
    "可以", "很好", "不错", "赞同", "我同意", "有道理", "继续", "完成", "明确",
    "清楚", "我们可以", "分工", "推进", "补充", "总结", "整理", "我负责", "你负责",
]

VALUE_DOUBT_WORDS = ["没意思", "无聊", "没劲", "没意义", "有用吗", "做给谁看", "做了也白做", "反正"]
PASSIVE_DETACHMENT_WORDS = ["随便", "随便吧", "都行", "听你们的", "不想说", "没什么想说", "没啥想说", "无所谓", "混"]
SELF_REGULATION_WORDS = [
    "回到主题", "回到任务", "回到正题", "我们跑题", "别跑题", "继续任务", "先确定",
    "总结一下", "重新分工", "先分工", "先整理", "说正事", "拉回来",
]
CONSTRUCTIVE_CONFLICT_WORDS = ["我理解", "可以结合", "换个角度", "有没有可能", "我们是不是", "不如", "共同点", "分歧点", "先看依据"]
EXECUTION_WORDS = ["我来", "你负责", "我负责", "分工", "整理", "汇总", "提交", "写文档", "做PPT", "负责"]
DEEP_THINKING_WORDS = ["我看看", "查资料", "我在看", "整理一下", "先想", "等我看", "这个资料", "这个案例", "我搜一下"]
COORDINATION_CONFUSION_WORDS = [
    "谁来", "谁负责", "怎么分工", "没人记录", "没人整理", "没人总结", "不知道干啥",
    "下一步干嘛", "接下来干嘛", "怎么推进", "乱了", "有点乱", "没有顺序", "没人管",
]

# Conflict recovery is interpreted as an ordered pattern, not as a one-keyword
# state.  These phrase groups provide evidence categories for that pattern.
CONFLICT_REPAIR_PHRASES = [
    "先别争", "别争了", "停止争论", "不要争论",
    "先别吵", "别吵了", "停止争吵",
    "不要互相否定", "先停止互相否定",
    "先冷静", "先听完", "先看依据", "先看证据", "先找共同点",
]
NON_DESTRUCTIVE_CONFLICT_PHRASES = CONFLICT_REPAIR_PHRASES + [
    "争取完成", "争取按时", "争取做好",
]
STRONG_CONFLICT_WORDS = [
    "你错", "不合理", "别说了", "没搞懂", "你没搞懂", "强行扯",
    "听我的", "按我的来", "想法不行", "你这个想法不行",
    "凭什么听你的", "谁听你的", "闭嘴", "你懂什么", "乱说",
    "别乱说", "你不懂", "别插嘴",
]
TASK_STRUCTURING_WORDS = [
    "制定标准", "比较标准", "评价标准", "判断标准", "重新分工",
    "先分工", "我负责", "你负责", "任务拆分", "拆解任务", "下一步",
]
EVIDENCE_COMPARISON_WORDS = [
    "补充证据", "比较证据", "对比证据", "看依据", "看证据",
    "根据数据", "补充数据", "补充案例", "比较方案", "对比方案",
]
CONSENSUS_SUMMARY_WORDS = [
    "形成结论", "得出结论", "阶段总结", "总结一下", "整理结论",
    "达成一致", "共同结论", "汇总结论", "最终结论",
]


def count_destructive_conflict_hits(text):
    """Count destructive signals after masking repair and benign “争” phrases."""
    masked = str(text or "")
    for phrase in sorted(NON_DESTRUCTIVE_CONFLICT_PHRASES, key=len, reverse=True):
        masked = masked.replace(phrase, "")
    return sum(1 for word in CONFLICT_WORDS if word and word in masked)

SERA_PROFILE = """
SERA assistant 是嵌入式旁观型协同学习情绪—协作调节智能体。
它不替代学生完成任务，不提供标准答案，只帮助小组识别、表达、调节和利用协作过程中的情绪。
实验组提示可以进行过程性引导，如轮流表达、观点澄清、任务拆解、角色分工、目标重聚焦、进度监控与总结反思。
对照组提示只提供一般性情绪支持，不提供明确共享调节步骤。
当小组处于深度思考、执行推进、建设性冲突或自发调节状态时，SERA 应保持观察，不主动打断。
"""
SERA_STRATEGY_VERSION = "SSRL_EMOTION_COLLAB_REGULATION_KB_v4"


def tpl(template_id, strategy_id, name, strategy_type, ssrl_phase, cognitive_load, text):
    return {
        "template_id": template_id,
        "strategy_id": strategy_id,
        "name": name,
        "type": strategy_type,
        "ssrl_phase": ssrl_phase,
        "cognitive_load": cognitive_load,
        "text": text,
    }

# -----------------------------
# 实验组：SSRL 智能情绪—协作调节话术库
# 话术结构：状态觉察 + 情绪调节 + 协作调节动作 + 任务推进
# -----------------------------
SERA_EXPERIMENT_TEMPLATES = {
    "positive": [
        tpl("EXP-POS-01", "SSRL-POS", "积极协作强化", "社会支持", "社会支持", "低", "你们现在的讨论比较积极，已经出现了补充和回应。接下来请一位成员总结目前形成的共识，其他成员补充一个证据或修改建议，让讨论成果更清楚。"),
        tpl("EXP-POS-02", "SSRL-POS", "积极协作强化", "社会支持", "共享监控", "低", "小组现在的互动节奏很好。为了把讨论沉淀下来，请记录员整理已经达成的结论，并标出还需要继续讨论的一个问题。"),
        tpl("EXP-POS-03", "SSRL-POS", "积极协作强化", "社会支持", "总结反思", "低", "你们刚才有观点提出，也有同伴回应，这是很好的协作方式。接下来可以用一句话确认：目前小组最认可的方案是什么？"),
        tpl("EXP-POS-04", "SSRL-POS", "积极协作强化", "社会支持", "进度监控", "低", "目前讨论比较顺利。建议你们检查一下：目标是否明确、依据是否充分、下一步是否有人负责。确认后继续推进。"),
        tpl("EXP-POS-05", "SSRL-POS", "积极协作强化", "社会支持", "观点共建", "低", "你们正在形成不错的共同想法。可以请每位成员再补充一个不同角度，让小组方案更完整。"),
        tpl("EXP-POS-06", "SSRL-POS", "积极协作强化", "社会支持", "共享监控", "低", "现在的讨论氛围比较好。请把这种方式保持下去：先说观点，再说理由，最后由一位成员做阶段性总结。"),
    ],
    "silence_no_text": [
        tpl("EXP-SIL-01", "SSRL-SIL", "在线沉默启动", "情绪觉察", "情绪觉察", "低", "我注意到小组聊天区暂时比较安静。现在不用马上给出完整答案，请每位成员轮流发送一句：“我现在最不确定的是……”。记录员把大家的困惑整理成下一步要解决的问题。"),
        tpl("EXP-SIL-02", "SSRL-SIL", "在线沉默启动", "情绪表达", "情绪外化", "低", "讨论暂时没有展开也没关系，可以先从一个小问题开始。请每位成员发送一句：“我认为我们下一步可以先做……”，然后小组选择最容易开始的一步。"),
        tpl("EXP-SIL-03", "SSRL-SIL", "在线沉默启动", "情绪表达", "共享监控", "低", "你们可能还在思考怎么开始。请先不用追求完整，每个人只发一个关键词：目标、资料、分工或困难。发完后再决定先处理哪一项。"),
        tpl("EXP-SIL-04", "SSRL-SIL", "在线沉默启动", "情绪觉察", "任务启动", "低", "小组现在可以先做一个启动动作：每位成员在聊天区写一句自己已经理解的任务要求。之后比较哪些地方一致、哪些地方还不清楚。"),
        tpl("EXP-SIL-05", "SSRL-SIL", "在线沉默启动", "社会支持", "参与启动", "低", "暂时安静不代表没有想法。请先让每个人贡献一个很小的内容：一个问题、一个例子、一个资料线索或一个担心点。"),
        tpl("EXP-SIL-06", "SSRL-SIL", "在线沉默启动", "情绪调节", "任务推进", "低", "如果一时不知道怎么说，可以先降低难度：请每位成员只完成半句话：“我觉得这个任务最关键的是……”。发完后小组再合并相近想法。"),
    ],
    "silence_low_interaction": [
        tpl("EXP-LINT-01", "SSRL-LINT", "低互动激活", "情绪表达", "参与均衡", "低", "目前聊天区有一些发言，但回应还没有完全展开。请下一位成员先回应前面同学的一句话：我同意的是……；我想补充的是……。"),
        tpl("EXP-LINT-02", "SSRL-LINT", "低互动激活", "社会支持", "回应建立", "低", "讨论已经开始了，但可以让回应更充分。请每位成员选择一个同伴观点，补充一个理由、例子或问题。"),
        tpl("EXP-LINT-03", "SSRL-LINT", "低互动激活", "情绪觉察", "参与启动", "低", "现在小组互动还比较少。可以先用“接龙”的方式推进：一个人说观点，下一个人补充依据，再下一个人提出疑问，最后一个人总结。"),
        tpl("EXP-LINT-04", "SSRL-LINT", "低互动激活", "社会支持", "观点共建", "低", "已有同学开始表达了。为了让讨论继续，请其他成员分别补充一个不同角度：原因、影响、资料或解决办法。"),
        tpl("EXP-LINT-05", "SSRL-LINT", "低互动激活", "情绪表达", "共享监控", "低", "如果大家还不确定，可以先把不确定说出来。请每人发送一句：“我还需要确认的是……”。这些问题会帮助小组确定下一步。"),
        tpl("EXP-LINT-06", "SSRL-LINT", "低互动激活", "社会支持", "任务推进", "低", "现在可以把零散发言连起来。请一位成员先整理已出现的想法，其他成员各补充一个支持或修改意见。"),
    ],
    "participation_imbalance": [
        tpl("EXP-IMB-01", "SSRL-IMB", "参与均衡支持", "社会支持", "参与均衡", "中", "刚才已经有同学提出了主要想法。现在请其他成员每人补充一个内容：一个证据、一个问题、一个风险或一个改进建议。这样可以让小组方案更完整。"),
        tpl("EXP-IMB-02", "SSRL-IMB", "参与均衡支持", "社会支持", "角色分担", "中", "小组讨论好像集中在少数成员身上了。可以临时分一下贡献方式：一人提观点，一人找依据，一人提问题，一人做总结。"),
        tpl("EXP-IMB-03", "SSRL-IMB", "参与均衡支持", "情绪表达", "表达邀请", "低", "每个人的想法都能帮助小组推进。请暂时还没发言的成员先发一句：我支持的点是……或我担心的点是……。"),
        tpl("EXP-IMB-04", "SSRL-IMB", "参与均衡支持", "社会支持", "协作推进", "中", "为了避免一个人承担太多，接下来请按顺序发言：观点、依据、疑问、总结。每个人完成其中一项即可。"),
        tpl("EXP-IMB-05", "SSRL-IMB", "参与均衡支持", "共享监控", "参与监控", "中", "请小组检查一下：目前谁的想法还没有被听到？可以先邀请这位成员补充一个问题或一个例子。"),
        tpl("EXP-IMB-06", "SSRL-IMB", "参与均衡支持", "社会支持", "心理安全", "低", "不完整的想法也可以先说出来。请每位成员贡献一个小片段，小组再一起把它们整理成完整方案。"),
    ],
    "conflict": [
        tpl("EXP-CON-01", "SSRL-CON", "冲突转化", "情绪调节", "观点澄清", "高", "你们现在可能出现了不同意见。分歧本身是有价值的，先不要判断谁对谁错。请把不同方案分别写出来，每个方案说出一个依据、一个优点和一个可能风险，再决定可以保留哪一部分。"),
        tpl("EXP-CON-02", "SSRL-CON", "冲突转化", "情绪调节", "心理安全", "中", "讨论有点紧张时，可以先把“人”和“观点”分开。请把表达从“你错了”改成“我担心这个方案可能会……”，再继续比较方案。"),
        tpl("EXP-CON-03", "SSRL-CON", "冲突转化", "情绪表达", "观点澄清", "中", "不同观点可以帮助小组想得更深。请每位成员先说一句：我同意对方观点中的一部分是……；我还想补充的是……。"),
        tpl("EXP-CON-04", "SSRL-CON", "冲突转化", "共享监控", "方案比较", "高", "如果两个方案都有人支持，可以先不急着选。请小组列出两个方案分别能解决什么问题，再比较哪个更符合任务目标。"),
        tpl("EXP-CON-05", "SSRL-CON", "冲突转化", "社会支持", "心理安全", "中", "现在先保护每个人的表达空间。请轮流说明自己的理由，其他成员先只记录关键词，等都说完后再回应。"),
        tpl("EXP-CON-06", "SSRL-CON", "冲突转化", "情绪调节", "共同目标", "中", "分歧说明大家都在投入思考。请先回到共同目标：你们希望最终成果解决什么问题？再看不同意见如何服务这个目标。"),
    ],
    "frustration": [
        tpl("EXP-FRU-01", "SSRL-FRU", "挫败调节", "情绪调节", "任务拆解", "中", "卡住不代表你们做错了，项目学习本来就会经历不确定阶段。请小组先写三栏：已经完成了什么、还缺什么、下一步最小行动是什么。先完成最小的一步，再继续推进。"),
        tpl("EXP-FRU-02", "SSRL-FRU", "挫败调节", "情绪表达", "困难外化", "低", "你们现在不需要一次性解决整个任务。请每位成员说一句：我们具体卡在哪一步？是任务理解、资料不足、分工不清，还是表达结果有困难？"),
        tpl("EXP-FRU-03", "SSRL-FRU", "挫败调节", "情绪调节", "控制感恢复", "中", "觉得难是有用信号，说明需要把问题变小。请先选一个最容易验证的小问题，完成后再决定下一步。"),
        tpl("EXP-FRU-04", "SSRL-FRU", "挫败调节", "共享监控", "任务推进", "中", "如果一直停在同一个点，可以换成流程检查：目标清楚吗？资料够吗？分工明确吗？先找出最影响推进的一项。"),
        tpl("EXP-FRU-05", "SSRL-FRU", "挫败调节", "社会支持", "共同承担", "低", "这个困难不是某一个人的问题，可以把它当成小组共同要解决的环节。请每人提出一个可能帮助推进的小办法。"),
        tpl("EXP-FRU-06", "SSRL-FRU", "挫败调节", "情绪调节", "最小行动", "中", "先不要追求完整成果。请小组确定一个 3 分钟内能完成的小动作，例如补一个例子、查一个资料或写一句结论。"),
    ],
    "low_motivation": [
        tpl("EXP-MOT-01", "SSRL-MOT", "意义重连", "情绪调节", "意义重连", "中", "如果觉得任务有点没意思，可以先换个角度：这个问题在现实中会影响谁？请每位成员说一个可能受影响的人或场景。"),
        tpl("EXP-MOT-02", "SSRL-MOT", "意义重连", "情绪调节", "价值重评", "中", "当任务显得很大时，可以先找一个和自己生活更接近的小切入点。请小组选择一个最熟悉的例子继续讨论。"),
        tpl("EXP-MOT-03", "SSRL-MOT", "意义重连", "共享监控", "目标重建", "中", "现在可以重新确认一下：你们希望这个项目最终产生什么价值？用一句话写出小组最想解决的问题。"),
        tpl("EXP-MOT-04", "SSRL-MOT", "意义重连", "社会支持", "参与启动", "低", "短暂提不起劲是正常的。请每个人先贡献一个自己愿意负责的小部分，让任务从一个小点重新启动。"),
    ],
    "offtask": [
        tpl("EXP-OFF-01", "SSRL-OFF", "任务重聚焦", "情绪调节", "目标监控", "中", "我注意到你们的讨论可能暂时偏离了任务。请先回到三个问题：最终要提交什么？现在已经完成什么？接下来 5 分钟先完成哪一步？把答案写在聊天区后再继续讨论。"),
        tpl("EXP-OFF-02", "SSRL-OFF", "任务重聚焦", "共享监控", "进度监控", "中", "可以先暂停当前话题，检查它和任务成果的关系。请小组确定一个 5 分钟小目标：资料、观点、结构或汇报内容中，先推进哪一项？"),
        tpl("EXP-OFF-03", "SSRL-OFF", "任务重聚焦", "情绪调节", "注意重定向", "低", "刚才的话题可以先记下来，等任务推进后再聊。现在请用一句话确认：我们目前讨论到任务的哪一步？"),
        tpl("EXP-OFF-04", "SSRL-OFF", "任务重聚焦", "共享监控", "目标重锚", "中", "如果讨论内容有点散，可以用任务目标来筛选：哪些内容和最终成果有关？请保留相关内容，暂时放下无关内容。"),
        tpl("EXP-OFF-05", "SSRL-OFF", "任务重聚焦", "协作推进", "短时计划", "中", "请小组把接下来 5 分钟只留给一个动作：确定观点、找依据、整理结构或准备汇报。先选一个继续。"),
        tpl("EXP-OFF-06", "SSRL-OFF", "任务重聚焦", "共享监控", "成果导向", "中", "可以回到最终成果来检查：现在缺少哪一部分？请一位成员说缺口，其他成员各补一个解决办法。"),
    ],
    "passive_detachment": [
        tpl("EXP-PAS-01", "SSRL-PAS", "敷衍脱离调节", "情绪表达", "参与启动", "低", "如果暂时没有想法，可以先从很小的贡献开始。请每人发一句：我能为下一步做的是……。"),
        tpl("EXP-PAS-02", "SSRL-PAS", "敷衍脱离调节", "社会支持", "责任分担", "中", "小组讨论不能只靠少数人推进。请每位成员选择一个角色：找资料、提问题、整理观点或总结结论。"),
        tpl("EXP-PAS-03", "SSRL-PAS", "敷衍脱离调节", "情绪调节", "控制感恢复", "低", "说“都行”有时代表还没找到切入点。请每位成员先选一个自己比较能完成的小任务，再一起合并。"),
        tpl("EXP-PAS-04", "SSRL-PAS", "敷衍脱离调节", "共享监控", "任务推进", "中", "现在请小组把责任具体化：谁来补资料？谁来整理观点？谁来做最后总结？先用临时分工让讨论动起来。"),
    ],
    "coordination": [
        tpl("EXP-COO-01", "SSRL-COO", "分工与推进支持", "协作推进", "角色分工", "中", "你们现在已经有一些想法，但需要更清楚的推进方式。可以先临时分工：一位主持人负责讨论顺序，一位记录员整理关键词，一位成员补充资料，一位成员准备总结。5 分钟后再合并大家的结果。"),
        tpl("EXP-COO-02", "SSRL-COO", "分工与推进支持", "共享监控", "进度监控", "中", "如果讨论有点乱，可以先确定顺序：目标是什么、已有信息是什么、还缺什么、下一步谁负责。请按这四项快速整理。"),
        tpl("EXP-COO-03", "SSRL-COO", "分工与推进支持", "协作推进", "角色分担", "中", "小组可以先设置四个临时角色：主持、记录、资料、汇报。角色不固定，先帮助你们把讨论推进起来。"),
        tpl("EXP-COO-04", "SSRL-COO", "分工与推进支持", "共享监控", "任务规划", "中", "现在最需要的是明确下一步。请小组只回答三个问题：谁来做？做什么？什么时候合并？"),
        tpl("EXP-COO-05", "SSRL-COO", "分工与推进支持", "协作推进", "成果整理", "中", "如果已经有很多想法，请先不要继续发散。请记录员整理关键词，其他成员分别判断哪些可以放进最终成果。"),
        tpl("EXP-COO-06", "SSRL-COO", "分工与推进支持", "共享监控", "推进结构", "中", "可以用一个简单流程继续：先确认目标，再分配任务，然后各自补充，最后汇总。请先完成第一步：确认目标。"),
    ],
    "protected": [
        tpl("EXP-OI-01", "SSRL-OI", "观察保护", "观察抑制", "观察抑制", "无", "小组正在进行建设性讨论、深度思考或自发调节，建议暂不打断，继续观察。"),
        tpl("EXP-OI-02", "SSRL-OI", "观察保护", "观察抑制", "观察抑制", "无", "小组已出现主动回到任务、分工执行或成果整理迹象，建议保护这种自发调节过程。"),
    ],
    "default": [
        tpl("EXP-DEF-01", "SSRL-DEF", "一般协作支持", "情绪—协作调节", "共享监控", "低", "请小组先检查当前讨论状态：目标是否清楚？每个人是否知道下一步？如果不清楚，可以先用一句话确认接下来要完成的小任务。"),
    ],
}

# -----------------------------
# 对照组：普通情绪支持话术库
# 话术结构：状态回应 + 情绪安慰 + 积极鼓励
# 不包含明确共享调节步骤。
# -----------------------------
SERA_CONTROL_TEMPLATES = {
    "positive": [
        tpl("CTL-POS-01", "CTRL-POS", "积极情绪支持", "普通情绪支持", "通用", "低", "你们现在的讨论状态很好，大家都比较投入。请继续保持这种积极的学习状态，加油！"),
        tpl("CTL-POS-02", "CTRL-POS", "积极情绪支持", "普通情绪支持", "通用", "低", "小组目前的讨论氛围比较积极，大家的投入值得肯定。希望你们继续保持这种状态。"),
        tpl("CTL-POS-03", "CTRL-POS", "积极情绪支持", "普通情绪支持", "通用", "低", "看到你们保持了不错的讨论节奏，这说明大家都在认真参与。继续加油！"),
        tpl("CTL-POS-04", "CTRL-POS", "积极情绪支持", "普通情绪支持", "通用", "低", "你们现在的合作氛围很好，保持信心，继续完成接下来的任务。"),
        tpl("CTL-POS-05", "CTRL-POS", "积极情绪支持", "普通情绪支持", "通用", "低", "大家的讨论状态值得肯定。请继续保持积极和耐心，相信你们会有不错的成果。"),
        tpl("CTL-POS-06", "CTRL-POS", "积极情绪支持", "普通情绪支持", "通用", "低", "你们已经展现出很好的投入状态。继续保持这种认真态度，稳步完成任务。"),
    ],
    "silence_no_text": [
        tpl("CTL-SIL-01", "CTRL-SIL", "沉默情绪支持", "普通情绪支持", "通用", "低", "我注意到小组讨论暂时变少了。讨论节奏慢一点是正常的，大家可能还在思考。请不要有压力，保持信心。"),
        tpl("CTL-SIL-02", "CTRL-SIL", "沉默情绪支持", "普通情绪支持", "通用", "低", "现在安静一些也没关系，思考本身也是学习的一部分。相信你们能够逐渐进入讨论状态。"),
        tpl("CTL-SIL-03", "CTRL-SIL", "沉默情绪支持", "普通情绪支持", "通用", "低", "讨论暂时停下来是可以理解的。请放松一些，保持积极心态，相信小组可以继续推进。"),
        tpl("CTL-SIL-04", "CTRL-SIL", "沉默情绪支持", "普通情绪支持", "通用", "低", "大家可能正在整理思路。不要着急，慢慢来，你们有能力完成这次任务。"),
        tpl("CTL-SIL-05", "CTRL-SIL", "沉默情绪支持", "普通情绪支持", "通用", "低", "短暂沉默并不代表讨论不好。请保持信心，继续以积极的状态面对任务。"),
        tpl("CTL-SIL-06", "CTRL-SIL", "沉默情绪支持", "普通情绪支持", "通用", "低", "如果现在还没有想好，也不用紧张。相信大家能够逐步找到讨论方向。"),
    ],
    "silence_low_interaction": [
        tpl("CTL-LINT-01", "CTRL-LINT", "低互动情绪支持", "普通情绪支持", "通用", "低", "小组已经开始讨论了，只是互动还不多。请大家保持耐心和信心，继续投入到任务中。"),
        tpl("CTL-LINT-02", "CTRL-LINT", "低互动情绪支持", "普通情绪支持", "通用", "低", "现在讨论节奏比较慢，这很正常。希望大家放轻松，继续保持积极参与的状态。"),
        tpl("CTL-LINT-03", "CTRL-LINT", "低互动情绪支持", "普通情绪支持", "通用", "低", "每个人进入讨论状态的节奏可能不同。请相信自己，也相信小组能够继续合作完成任务。"),
        tpl("CTL-LINT-04", "CTRL-LINT", "低互动情绪支持", "普通情绪支持", "通用", "低", "目前互动还可以再活跃一些。不要担心表达得是否完美，保持信心继续讨论就好。"),
        tpl("CTL-LINT-05", "CTRL-LINT", "低互动情绪支持", "普通情绪支持", "通用", "低", "讨论有时需要一点时间热起来。请大家保持积极心态，慢慢进入更好的合作状态。"),
        tpl("CTL-LINT-06", "CTRL-LINT", "低互动情绪支持", "普通情绪支持", "通用", "低", "现在的讨论还在启动阶段。请不要有压力，相信你们能够逐渐形成更顺畅的交流。"),
    ],
    "participation_imbalance": [
        tpl("CTL-IMB-01", "CTRL-IMB", "参与情绪支持", "普通情绪支持", "通用", "低", "每个人的参与节奏可能不同，这很正常。相信每位成员都有自己的优势，也都能为小组带来价值。"),
        tpl("CTL-IMB-02", "CTRL-IMB", "参与情绪支持", "普通情绪支持", "通用", "低", "小组合作需要大家共同努力。希望大家保持信心，继续为讨论贡献自己的想法。"),
        tpl("CTL-IMB-03", "CTRL-IMB", "参与情绪支持", "普通情绪支持", "通用", "低", "有的同学表达得多一些，有的同学还在思考，这都是正常的。请保持积极和耐心。"),
        tpl("CTL-IMB-04", "CTRL-IMB", "参与情绪支持", "普通情绪支持", "通用", "低", "每个人都可能用不同方式参与合作。相信大家都能在讨论中发挥自己的作用。"),
        tpl("CTL-IMB-05", "CTRL-IMB", "参与情绪支持", "普通情绪支持", "通用", "低", "请大家继续保持友好和支持的氛围。小组合作中，每个人的投入都很重要。"),
        tpl("CTL-IMB-06", "CTRL-IMB", "参与情绪支持", "普通情绪支持", "通用", "低", "不同的参与节奏并不影响大家共同完成任务。请继续保持信心和合作态度。"),
    ],
    "conflict": [
        tpl("CTL-CON-01", "CTRL-CON", "冲突情绪支持", "普通情绪支持", "通用", "中", "讨论中出现不同意见是正常的，说明大家都在认真思考。请保持耐心和尊重，相信小组可以找到合适的方向。"),
        tpl("CTL-CON-02", "CTRL-CON", "冲突情绪支持", "普通情绪支持", "通用", "中", "有分歧并不代表合作不好。希望大家保持平和心态，用积极的态度继续面对任务。"),
        tpl("CTL-CON-03", "CTRL-CON", "冲突情绪支持", "普通情绪支持", "通用", "中", "观点不同是小组讨论中很常见的情况。请大家放松一些，相信你们能够处理好分歧。"),
        tpl("CTL-CON-04", "CTRL-CON", "冲突情绪支持", "普通情绪支持", "通用", "中", "现在可能有些紧张。请尽量保持友好和耐心，继续以合作的态度完成任务。"),
        tpl("CTL-CON-05", "CTRL-CON", "冲突情绪支持", "普通情绪支持", "通用", "中", "不同想法本身是有价值的。希望大家彼此尊重，保持积极沟通。"),
        tpl("CTL-CON-06", "CTRL-CON", "冲突情绪支持", "普通情绪支持", "通用", "中", "遇到分歧时不要着急。请保持信心，相信小组能够逐渐形成共同方向。"),
    ],
    "frustration": [
        tpl("CTL-FRU-01", "CTRL-FRU", "挫败情绪支持", "普通情绪支持", "通用", "低", "遇到困难是学习过程中很正常的事情，不要因为暂时卡住就否定自己。请保持信心，你们有能力继续完成任务。"),
        tpl("CTL-FRU-02", "CTRL-FRU", "挫败情绪支持", "普通情绪支持", "通用", "低", "现在觉得难是可以理解的。请不要着急，慢慢来，相信你们能够找到解决办法。"),
        tpl("CTL-FRU-03", "CTRL-FRU", "挫败情绪支持", "普通情绪支持", "通用", "低", "暂时没有思路并不可怕，这是项目学习中常见的阶段。请保持耐心和积极心态。"),
        tpl("CTL-FRU-04", "CTRL-FRU", "挫败情绪支持", "普通情绪支持", "通用", "低", "卡住的时候容易产生压力，但这不代表你们做得不好。请放松一些，继续尝试。"),
        tpl("CTL-FRU-05", "CTRL-FRU", "挫败情绪支持", "普通情绪支持", "通用", "低", "学习任务有挑战是正常的。相信你们可以相互支持，逐渐克服困难。"),
        tpl("CTL-FRU-06", "CTRL-FRU", "挫败情绪支持", "普通情绪支持", "通用", "低", "请不要因为一时的困难失去信心。你们已经在努力了，继续保持积极状态。"),
    ],
    "low_motivation": [
        tpl("CTL-MOT-01", "CTRL-MOT", "低动机情绪支持", "普通情绪支持", "通用", "低", "有时任务会让人觉得提不起劲，这是可以理解的。请大家调整一下状态，继续保持积极参与。"),
        tpl("CTL-MOT-02", "CTRL-MOT", "低动机情绪支持", "普通情绪支持", "通用", "低", "如果现在觉得有些无聊，也不要太着急。相信你们可以慢慢找回讨论状态。"),
        tpl("CTL-MOT-03", "CTRL-MOT", "低动机情绪支持", "普通情绪支持", "通用", "低", "学习过程中状态起伏很正常。请保持信心，继续以认真态度完成任务。"),
        tpl("CTL-MOT-04", "CTRL-MOT", "低动机情绪支持", "普通情绪支持", "通用", "低", "暂时兴趣不高也没关系。希望大家互相鼓励，继续坚持完成讨论。"),
    ],
    "offtask": [
        tpl("CTL-OFF-01", "CTRL-OFF", "任务脱离情绪支持", "普通情绪支持", "通用", "低", "讨论过程中偶尔分散注意力是正常的。请大家保持积极状态，相信你们可以重新投入到学习任务中。"),
        tpl("CTL-OFF-02", "CTRL-OFF", "任务脱离情绪支持", "普通情绪支持", "通用", "低", "你们已经投入了不少讨论精力。希望大家继续保持认真态度，把注意力慢慢拉回到学习中。"),
        tpl("CTL-OFF-03", "CTRL-OFF", "任务脱离情绪支持", "普通情绪支持", "通用", "低", "有时候讨论会暂时偏离方向，这很常见。请大家调整状态，继续保持学习投入。"),
        tpl("CTL-OFF-04", "CTRL-OFF", "任务脱离情绪支持", "普通情绪支持", "通用", "低", "短暂放松是可以理解的。希望大家继续保持积极心态，重新进入讨论。"),
        tpl("CTL-OFF-05", "CTRL-OFF", "任务脱离情绪支持", "普通情绪支持", "通用", "低", "现在可能有点分散。请不要有压力，慢慢把状态调整回来就好。"),
        tpl("CTL-OFF-06", "CTRL-OFF", "任务脱离情绪支持", "普通情绪支持", "通用", "低", "小组讨论有时会出现节奏变化。相信大家可以继续保持认真和投入。"),
    ],
    "passive_detachment": [
        tpl("CTL-PAS-01", "CTRL-PAS", "敷衍脱离情绪支持", "普通情绪支持", "通用", "低", "暂时没有想法是可以理解的。请不要有压力，保持信心，继续参与小组学习。"),
        tpl("CTL-PAS-02", "CTRL-PAS", "敷衍脱离情绪支持", "普通情绪支持", "通用", "低", "每个人都会有状态不高的时候。请调整一下心态，相信自己可以继续为小组贡献力量。"),
        tpl("CTL-PAS-03", "CTRL-PAS", "敷衍脱离情绪支持", "普通情绪支持", "通用", "低", "说不出想法时不用着急。请保持积极和耐心，慢慢进入讨论状态。"),
        tpl("CTL-PAS-04", "CTRL-PAS", "敷衍脱离情绪支持", "普通情绪支持", "通用", "低", "合作学习需要一点时间进入状态。相信大家都能逐渐参与进来。"),
    ],
    "coordination": [
        tpl("CTL-COO-01", "CTRL-COO", "协作困难情绪支持", "普通情绪支持", "通用", "低", "小组合作有时会需要一点时间进入状态。请大家保持耐心和信心，相信你们可以逐渐找到合适的合作方式。"),
        tpl("CTL-COO-02", "CTRL-COO", "协作困难情绪支持", "普通情绪支持", "通用", "低", "如果现在感觉讨论有点乱，也不要着急。合作本来就需要磨合，相信你们能够慢慢调整好。"),
        tpl("CTL-COO-03", "CTRL-COO", "协作困难情绪支持", "普通情绪支持", "通用", "低", "小组推进过程中出现不清楚的地方很正常。请保持积极心态，继续相互支持。"),
        tpl("CTL-COO-04", "CTRL-COO", "协作困难情绪支持", "普通情绪支持", "通用", "低", "团队合作需要大家共同适应。相信你们可以逐渐形成更顺畅的讨论状态。"),
        tpl("CTL-COO-05", "CTRL-COO", "协作困难情绪支持", "普通情绪支持", "通用", "低", "现在可能还在磨合阶段。请保持信心和耐心，继续完成这次学习任务。"),
        tpl("CTL-COO-06", "CTRL-COO", "协作困难情绪支持", "普通情绪支持", "通用", "低", "合作过程中遇到一些小困难是正常的。请大家保持友好和积极的态度。"),
    ],
    "protected": [
        tpl("CTL-OI-01", "CTRL-OI", "观察保护", "普通情绪支持", "通用", "无", "小组目前处于相对稳定的讨论或思考状态，暂时不需要额外提示。"),
    ],
    "default": [
        tpl("CTL-DEF-01", "CTRL-DEF", "一般情绪支持", "普通情绪支持", "通用", "低", "请大家稳定一下状态，继续保持沟通就好。别有压力，保持积极和耐心，一起加油。"),
    ],
}

# 子状态路由表。template_key 对应上面两个话术库中的 key。
SERA_ROUTE_TABLE = {
    "标准型": {"template_key": "default", "should_intervene": False},
    "积极协作型": {"template_key": "positive", "should_intervene": True},
    "深度思考": {"template_key": "protected", "should_intervene": False},
    "执行推进": {"template_key": "protected", "should_intervene": False},
    "建设性冲突": {"template_key": "protected", "should_intervene": False},
    "人际性冲突": {"template_key": "conflict", "should_intervene": True},
    "困惑型": {"template_key": "silence_low_interaction", "should_intervene": True},
    "在线沉默-无人发言": {"template_key": "silence_no_text", "should_intervene": True},
    "在线沉默-低互动": {"template_key": "silence_low_interaction", "should_intervene": True},
    "一人主导型沉默": {"template_key": "participation_imbalance", "should_intervene": True},
    "参与不均": {"template_key": "participation_imbalance", "should_intervene": True},
    "挫败型": {"template_key": "frustration", "should_intervene": True},
    "倦怠型": {"template_key": "low_motivation", "should_intervene": True},
    "跑题脱离(有自调节)": {"template_key": "protected", "should_intervene": False},
    "跑题脱离(无自调节)": {"template_key": "offtask", "should_intervene": True},
    "敷衍脱离": {"template_key": "passive_detachment", "should_intervene": True},
    "分工混乱": {"template_key": "coordination", "should_intervene": True},
    "推进无序": {"template_key": "coordination", "should_intervene": True},
}

# 兼容旧版 strategy.py / 其他模块可能引用的变量名。
# 新版 agent/strategy.py 主要使用 SERA_EXPERIMENT_TEMPLATES / SERA_CONTROL_TEMPLATES。
SERA_STRATEGY_KB = {}
for bank in (SERA_EXPERIMENT_TEMPLATES, SERA_CONTROL_TEMPLATES):
    for items in bank.values():
        for item in items:
            sid = item["strategy_id"]
            if sid not in SERA_STRATEGY_KB:
                SERA_STRATEGY_KB[sid] = {
                    "name": item["name"],
                    "type": item["type"],
                    "ssrl_phase": item["ssrl_phase"],
                    "cognitive_load": item["cognitive_load"],
                    "templates": [item["text"]],
                }
            else:
                SERA_STRATEGY_KB[sid]["templates"].append(item["text"])

GENERIC_TEMPLATES = {
    key: [item["text"] for item in items]
    for key, items in SERA_CONTROL_TEMPLATES.items()
}
GENERIC_TEMPLATES.update({
    "积极协作型": GENERIC_TEMPLATES.get("positive", []),
    "困惑型": GENERIC_TEMPLATES.get("silence_low_interaction", []),
    "挫败型": GENERIC_TEMPLATES.get("frustration", []),
    "倦怠型": GENERIC_TEMPLATES.get("low_motivation", []),
    "跑题脱离(无自调节)": GENERIC_TEMPLATES.get("offtask", []),
    "敷衍脱离": GENERIC_TEMPLATES.get("passive_detachment", []),
    "人际性冲突": GENERIC_TEMPLATES.get("conflict", []),
    "default": GENERIC_TEMPLATES.get("default", ["稳定一下状态，继续保持沟通就好。"]),
})
