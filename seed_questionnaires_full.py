"""Seed comprehensive test questionnaires for SSRL-ESP."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import config
from db import create_questionnaire, query_one

QUESTIONNAIRES = [
    # ===== SSRL =====
    {
        "code": "ssrl_pretest_v1",
        "category_key": "ssrl",
        "title": "SSRL 前测 - 自我调节学习策略认知",
        "description": "请在开始小组讨论前完成此问卷，评估你对自我调节学习策略的认知与准备情况。",
        "timing": "pre",
        "scale_max": 5,
        "active": True,
        "sort_order": 1,
        "items": [
            {"prompt_text": "在小组合作中，我有明确的个人学习目标和计划", "dimension_label": "目标设定"},
            {"prompt_text": "我能够监控自己在小组合作中的学习进度", "dimension_label": "自我监控"},
            {"prompt_text": "合作学习时，我有意识地调整自己的学习策略", "dimension_label": "策略调整"},
            {"prompt_text": "我会反思自己在小组中的学习效果并总结经验", "dimension_label": "自我反思"},
            {"prompt_text": "我能够协调个人目标与小组目标之间的关系", "dimension_label": "协同调节"},
        ],
    },
    {
        "code": "ssrl_posttest_v1",
        "category_key": "ssrl",
        "title": "SSRL 后测 - 自我调节学习策略使用体验",
        "description": "请在完成小组讨论和成果提交后完成此问卷，回顾你在本次合作中自我调节学习策略的使用情况。",
        "timing": "post",
        "scale_max": 5,
        "active": True,
        "sort_order": 2,
        "items": [
            {"prompt_text": "在本次小组合作中，我实现了自己设定的学习目标", "dimension_label": "目标实现"},
            {"prompt_text": "合作过程中，我清楚自己的学习进度并及时调整", "dimension_label": "过程监控"},
            {"prompt_text": "遇到困难时，我能够主动调整学习策略来应对", "dimension_label": "策略灵活性"},
            {"prompt_text": "我通过反思本次合作经历获得了有意义的收获", "dimension_label": "反思深度"},
            {"prompt_text": "我能够有效平衡个人学习需求和小组协作要求", "dimension_label": "双重调节"},
        ],
    },
    # ===== 协作情绪 =====
    {
        "code": "collab_emotion_pre_v1",
        "category_key": "collaboration_emotion",
        "title": "协作情绪前测 - 小组合作情绪预期",
        "description": "请在开始小组合作前填写，描述你此刻对协作的情绪预期。",
        "timing": "pre",
        "scale_max": 5,
        "active": True,
        "sort_order": 3,
        "items": [
            {"prompt_text": "我对即将开始的小组合作感到期待和积极", "dimension_label": "积极期待"},
            {"prompt_text": "我担心小组合作中会出现分歧或冲突", "dimension_label": "消极预期（反向）"},
            {"prompt_text": "与同伴一起学习让我感到更有动力", "dimension_label": "社会促进"},
            {"prompt_text": "对于小组合作任务，我感到有些焦虑", "dimension_label": "任务焦虑（反向）"},
        ],
    },
    {
        "code": "collab_emotion_post_v1",
        "category_key": "collaboration_emotion",
        "title": "协作情绪后测 - 小组合作情绪体验",
        "description": "请在完成小组合作后填写，回顾你在本次协作中的真实情绪体验。",
        "timing": "post",
        "scale_max": 5,
        "active": True,
        "sort_order": 4,
        "items": [
            {"prompt_text": "在小组讨论中我感到轻松和愉快", "dimension_label": "积极情绪"},
            {"prompt_text": "合作过程中出现过让我感到紧张或不愉快的时刻", "dimension_label": "消极情绪（反向）"},
            {"prompt_text": "同伴的支持和鼓励让我更愿意参与讨论", "dimension_label": "情绪支持"},
            {"prompt_text": "完成小组合作后，我感到有成就感和满足感", "dimension_label": "成就感"},
        ],
    },
    # ===== 心理安全感 =====
    {
        "code": "psych_safety_pre_v1",
        "category_key": "psychological_safety",
        "title": "心理安全感前测 - 对小组心理安全氛围的预期",
        "description": "请在开始合作前填写，评估你对小组心理安全氛围的预期。",
        "timing": "pre",
        "scale_max": 5,
        "active": True,
        "sort_order": 5,
        "items": [
            {"prompt_text": "我相信在小组中可以自由表达自己的想法而不被嘲笑", "dimension_label": "表达安全"},
            {"prompt_text": "即使提出不同意见，我也相信同伴会尊重我", "dimension_label": "异议安全"},
            {"prompt_text": "遇到不懂的问题时，我敢于向同伴求助", "dimension_label": "求助安全"},
            {"prompt_text": "我觉得在小组中犯错是可以被接受的", "dimension_label": "容错氛围"},
        ],
    },
    {
        "code": "psych_safety_post_v1",
        "category_key": "psychological_safety",
        "title": "心理安全感后测 - 小组心理安全氛围体验",
        "description": "请在完成合作后填写，回顾你在本次合作中真实感受到的心理安全程度。",
        "timing": "post",
        "scale_max": 5,
        "active": True,
        "sort_order": 6,
        "items": [
            {"prompt_text": "在本次合作中，我能够自由地表达真实想法", "dimension_label": "表达安全"},
            {"prompt_text": "即使我的观点和同伴不同，我仍然愿意说出来", "dimension_label": "异议勇气"},
            {"prompt_text": "遇到困难时，我放心地向同伴寻求了帮助", "dimension_label": "求助行为"},
            {"prompt_text": "在小组中犯错时，我感受到了同伴的理解而非指责", "dimension_label": "容错体验"},
        ],
    },
    # ===== TAM 技术接受度 =====
    {
        "code": "tam_v1",
        "category_key": "tam",
        "title": "技术接受度问卷 - SERA 智能辅助系统使用体验",
        "description": "请根据你与 SERA 智能辅助系统的互动体验完成此问卷。",
        "timing": "post",
        "scale_max": 7,
        "active": True,
        "sort_order": 7,
        "items": [
            {"prompt_text": "SERA 辅助系统易于使用，界面直观", "dimension_label": "感知易用性"},
            {"prompt_text": "SERA 提供的建议和提示对我的学习有帮助", "dimension_label": "感知有用性"},
            {"prompt_text": "我愿意在今后的学习中继续使用 SERA 系统", "dimension_label": "使用意愿"},
            {"prompt_text": "SERA 系统增强了我的小组合作学习体验", "dimension_label": "整体态度"},
        ],
    },
    # ===== 认知负荷 =====
    {
        "code": "cognitive_load_v1",
        "category_key": "cognitive_load",
        "title": "认知负荷问卷 - 学习任务与辅助系统",
        "description": "请根据本次合作学习中的真实感受回答以下问题。",
        "timing": "post",
        "scale_max": 7,
        "active": True,
        "sort_order": 8,
        "items": [
            {"prompt_text": "本次小组合作学习任务的内容非常复杂，需要花费大量精力", "dimension_label": "内在认知负荷"},
            {"prompt_text": "在学习过程中，同时关注讨论内容、同伴发言和SERA提示让我感到负担很重", "dimension_label": "外在认知负荷"},
            {"prompt_text": "尽管任务有挑战性，但我通过努力能够理解并完成", "dimension_label": "相关认知负荷"},
        ],
    },
    # ===== 智能体干扰感 =====
    {
        "code": "agent_intrusion_v1",
        "category_key": "agent_intrusion",
        "title": "智能体干扰感问卷 - SERA 干预频率与时机",
        "description": "请根据你与 SERA 智能辅助系统互动的感受回答以下问题。",
        "timing": "post",
        "scale_max": 7,
        "active": True,
        "sort_order": 9,
        "items": [
            {"prompt_text": "SERA 智能体的干预提示出现的频率适中，没有干扰我的思路", "dimension_label": "干预频率（反向）"},
            {"prompt_text": "SERA 的提示出现在合适的时机，对我的讨论有实质性帮助", "dimension_label": "干预时机"},
            {"prompt_text": "我感觉 SERA 的存在让小组讨论更加自然和高效", "dimension_label": "存在感"},
            {"prompt_text": "SERA 的干预内容与我们的讨论话题是相关的", "dimension_label": "内容相关"},
        ],
    },
]

def questionnaire_exists(code):
    return query_one("SELECT id FROM questionnaires WHERE code=?", (code,))

def create_if_not_exists(payload):
    code = payload.get("code", "")
    if questionnaire_exists(code):
        print(f"  [SKIP] Questionnaire '{code}' already exists.")
        return None
    items = payload.pop("items", [])
    qid = create_questionnaire(payload, items)
    print(f"  [OK] Created: {payload['title']} (id={qid}, code={code})")
    return qid

print("=== Seeding comprehensive test questionnaires ===\n")

for q_data in QUESTIONNAIRES:
    create_if_not_exists(q_data)

print(f"\nDone! Created {len(QUESTIONNAIRES)} questionnaires.")
print("Restart the Flask server to see the questionnaires in the teacher dashboard.")

