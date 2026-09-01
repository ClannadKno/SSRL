"""Seed mock pretest and posttest questionnaires for SSRL-ESP."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

# Must import config first to set up env
import config
from db import create_questionnaire, now_str, execute, query_one

PRETEST_ITEMS = [
    {"prompt_text": "我对本次小组合作学习任务有清晰的了解", "dimension_label": "任务理解"},
    {"prompt_text": "我相信小组成员能够有效协作完成学习任务", "dimension_label": "协作信心"},
    {"prompt_text": "我对本次学习主题已经有一定的基础知识", "dimension_label": "先前知识"},
    {"prompt_text": "我愿意在小组讨论中积极表达自己的观点", "dimension_label": "表达意愿"},
    {"prompt_text": "我期待通过小组讨论能加深对学习内容的理解", "dimension_label": "学习期待"},
]

POSTTEST_ITEMS = [
    {"prompt_text": "通过小组讨论，我对任务的理解更加深入了", "dimension_label": "任务理解"},
    {"prompt_text": "小组成员的讨论有效地帮助我解决了学习中的困惑", "dimension_label": "协作效果"},
    {"prompt_text": "我在小组讨论中充分表达了自己的观点", "dimension_label": "参与程度"},
    {"prompt_text": "小组合作中的不同观点促进了我的深入思考", "dimension_label": "认知冲突"},
    {"prompt_text": "我对本次小组合作学习的整体体验感到满意", "dimension_label": "整体评价"},
]

def questionnaire_exists(code):
    return query_one("SELECT id FROM questionnaires WHERE code=?", (code,))

def create_if_not_exists(payload):
    code = payload.get("code", "")
    if questionnaire_exists(code):
        print(f"  Questionnaire '{code}' already exists, skipping.")
        return None
    q = create_questionnaire(payload)
    print(f"  Created: {q['title']} (id={q['id']}, timing={q['timing']})")
    return q

print("=== Seeding mock questionnaires ===\n")

# Pretest
create_if_not_exists({
    "code": "mock_pretest_v1",
    "category_key": "ssrl",
    "title": "研究前测问卷（模拟）",
    "description": "请在开始小组讨论前完成此问卷。",
    "timing": "pre",
    "scale_max": 5,
    "active": True,
    "sort_order": 1,
    "items": PRETEST_ITEMS,
})

# Posttest
create_if_not_exists({
    "code": "mock_posttest_v1",
    "category_key": "ssrl",
    "title": "研究后测问卷（模拟）",
    "description": "请在完成小组讨论和成果提交后完成此问卷。",
    "timing": "post",
    "scale_max": 5,
    "active": True,
    "sort_order": 2,
    "items": POSTTEST_ITEMS,
})

print("\nDone! Restart the Flask server to see the questionnaires.")
