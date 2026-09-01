# -*- coding: utf-8 -*-
"""Seed the formal questionnaires from docs/questionnaire into the database.

The script is idempotent: existing questionnaires with the same code are
updated in place and their items are replaced.  It is safe to run after a
database rebuild or against an existing local database.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import config  # noqa: F401  # ensure DB path/env defaults are loaded
from db import db, ensure_database_ready, now_str
from migrations import run_pending_migrations


LIKERT_7_LABELS = ["从不", "极少", "很少", "偶尔", "有时", "经常", "总是"]
LIKERT_5_EMOTION_LABELS = ["非常不同意", "比较不同意", "一般", "比较同意", "非常同意"]
LIKERT_5_AGREE_LABELS = ["非常不同意", "不同意", "中立", "同意", "非常同意"]


def _j(value):
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _likert_item(code, text, dimension_key, dimension_label, order, *,
                 scale_max=5, labels=None, reverse=False, section_no=1,
                 section_title=""):
    return {
        "item_code": code,
        "prompt_text": text,
        "dimension_key": dimension_key,
        "dimension_label": dimension_label,
        "sort_order": order,
        "question_type": "likert_7" if scale_max == 7 else "likert_5",
        "min_value": 1,
        "max_value": scale_max,
        "reverse_scored": reverse,
        "include_in_score": True,
        "required": True,
        "section_no": section_no,
        "section_title": section_title,
        "scale_labels_json": _j(labels or (LIKERT_7_LABELS if scale_max == 7 else LIKERT_5_AGREE_LABELS)),
    }


SSRL_DIMENSIONS = {
    "goal": ("shared_goal_plan", "共享目标与计划"),
    "monitor": ("shared_monitor_feedback", "共享监控与反馈"),
    "reflect": ("shared_reflect_evaluate", "共享反思与评价"),
    "motivation": ("shared_motivation_emotion", "共享动机与情绪调节"),
}


SSRL_SOURCE_ITEMS = [
    ("SSRL1", "goal", "我会与小组成员共同设定清晰的学习目标。"),
    ("SSRL3", "goal", "当我们小组未能达成学习目标时，我会主动与其他成员一起调整学习策略。"),
    ("SSRL4", "goal", "在确立小组目标时，我会确保每位成员的想法都被考虑。"),
    ("SSRL5", "goal", "我会与小组成员共同制定衡量小组学习成效的标准。"),
    ("SSRL6", "monitor", "我会定期了解小组成员的学习进展。"),
    ("SSRL7", "monitor", "我会向小组成员提供建设性反馈，以帮助改进小组成果。"),
    ("SSRL8", "monitor", "当我遇到困难时，我会及时告知小组成员。"),
    ("SSRL9", "monitor", "我会评价小组的表现，以确保我们保持在正确的任务方向上。"),
    ("SSRL10", "monitor", "当小组实际做法偏离原定计划时，我会与小组成员公开讨论。"),
    ("SSRL11", "reflect", "完成任务后，我会与小组成员一起反思我们学到了什么。"),
    ("SSRL12", "reflect", "我会与小组成员讨论哪些策略有效、哪些策略无效。"),
    ("SSRL13", "reflect", "我会与小组成员一起分析小组协作成果。"),
    ("SSRL14", "reflect", "我很少与小组成员一起反思如何在以后的任务中改进协作。"),
    ("SSRL17", "motivation", "我会以积极的方式帮助小组成员改进学习策略。"),
    ("SSRL18", "motivation", "我更关注实现自己的个人目标。"),
    ("SSRL19", "motivation", "面对新的信息或变化时，我会与小组成员动态调整计划。"),
    ("SSRL22", "motivation", "我会积极参与小组的协作决策过程。"),
    ("SSRL24", "motivation", "当小组内部出现分歧时，我倾向于避免与成员互动。"),
]


def build_ssrl_items(prefix):
    items = []
    lead = "在以往的小组协作学习中，" if prefix == "pre" else "在本次小组协作学习中，"
    for idx, (code, dim_key, text) in enumerate(SSRL_SOURCE_ITEMS, start=1):
        key, label = SSRL_DIMENSIONS[dim_key]
        items.append(_likert_item(
            code,
            lead + text,
            key,
            label,
            idx,
            scale_max=7,
            labels=LIKERT_7_LABELS,
            reverse=code in {"SSRL14", "SSRL18", "SSRL24"},
            section_title=label,
        ))
    return items


EMOTION_ITEMS = [
    ("XQB1", "neg_expression", "消极情绪表达", "当我因时间冲突、能力差距或任务压力而感到心烦时，我会向小组成员表达焦虑。"),
    ("XQB2", "neg_expression", "消极情绪表达", "当我完成的任务没有达到要求时，我会向小组成员表达羞愧或不好意思的感受。"),
    ("XQB5", "neg_expression", "消极情绪表达", "当结果没有达到我的期望时，我会向小组成员表达失望的感受。"),
    ("JQP2", "pos_appraisal", "积极情绪评价", "当我认可小组成员的观点、建议或行为时，我会向其表达赞赏。"),
    ("JQP3", "pos_appraisal", "积极情绪评价", "当我获得小组成员的帮助时，我会表达感谢。"),
    ("JQP4", "pos_appraisal", "积极情绪评价", "为了促进小组成员积极互动，我会向小组成员表达鼓励。"),
    ("JQH1", "pos_response", "积极情绪回应", "当我认同小组成员的观点时，我会表达同意。"),
    ("JQH2", "pos_response", "积极情绪回应", "为了与小组成员达成一致，我会表达理解。"),
    ("JQH3", "pos_response", "积极情绪回应", "当我接纳小组成员的观点时，我会表达接受。"),
    ("JQB2", "pos_expression", "积极情绪表达", "当我喜欢协作过程时，我会表达希望继续参与这类协作学习活动的想法。"),
    ("JQB3", "pos_expression", "积极情绪表达", "当我认为自己某项任务做得不错时，我会向小组成员表达自豪感。"),
    ("JQB4", "pos_expression", "积极情绪表达", "当我感觉自己能够胜任任务时，我会向小组成员表达轻松或放松的感受。"),
    ("XQH1", "neg_response", "消极情绪回应", "当我不赞同小组成员的观点时，我会表达不同意的态度。"),
    ("XQH2", "neg_response", "消极情绪回应", "当我不想帮助他人时，我会表达拒绝帮助的态度。"),
    ("XQH3", "neg_response", "消极情绪回应", "当我不想承担某项任务时，我会向小组成员表达自己能力不足。"),
]


def build_emotion_items(prefix):
    lead = "在以往的小组协作学习中，" if prefix == "pre" else "在小组协作学习过程中，"
    return [
        _likert_item(code, lead + text, key, label, idx, scale_max=5,
                     labels=LIKERT_5_EMOTION_LABELS, section_title=label)
        for idx, (code, key, label, text) in enumerate(EMOTION_ITEMS, start=1)
    ]


SJT_ITEMS = [
    ("SJT1", "目标共识", "小组成员对任务目标的理解不一致，你会：",
     [("A", "按自己的理解先做"), ("B", "请一名成员作决定"), ("C", "简单询问大家是否同意"), ("D", "请成员说明理解并共同确认")],
     {"A": 0, "B": 1, "C": 2, "D": 3}),
    ("SJT2", "共同计划", "大家知道任务要求，但迟迟没有开始，你会：",
     [("A", "各自先完成擅长的部分"), ("B", "共同确定步骤、分工和时间"), ("C", "请一名成员直接分配任务"), ("D", "继续讨论，稍后再安排")],
     {"A": 0, "C": 1, "D": 2, "B": 3}),
    ("SJT3", "执行协调", "讨论主要由少数成员推进，其他人很少参与，你会：",
     [("A", "让发言多的成员继续推进"), ("B", "询问其他成员是否同意"), ("C", "请大家表达观点并重新协调"), ("D", "请较少发言者简单补充")],
     {"A": 0, "B": 1, "D": 2, "C": 3}),
    ("SJT4", "共享监控", "小组讨论了一段时间，但不清楚还缺什么，你会：",
     [("A", "共同对照要求检查进度"), ("B", "提醒大家抓紧时间"), ("C", "请一名成员负责检查"), ("D", "继续讨论，最后再检查")],
     {"D": 0, "C": 1, "B": 2, "A": 3}),
    ("SJT5", "策略调整", "当前方法无法推动任务，讨论开始重复，你会：",
     [("A", "再坚持使用原来的方法"), ("B", "直接采用某成员的新方法"), ("C", "先跳过困难部分"), ("D", "共同分析原因并尝试新方法")],
     {"A": 0, "B": 1, "C": 2, "D": 3}),
    ("SJT6", "评价反思", "小组已经形成初步成果，还有时间修改，你会：",
     [("A", "直接提交当前成果"), ("B", "共同检查、修改并总结经验"), ("C", "请一名成员负责修改"), ("D", "各自检查自己负责的部分")],
     {"A": 0, "C": 1, "D": 2, "B": 3}),
]


def build_sjt_items():
    items = []
    for idx, (code, label, prompt, options, score_map) in enumerate(SJT_ITEMS, start=1):
        items.append({
            "item_code": code,
            "prompt_text": prompt,
            "dimension_key": "collab_sjt",
            "dimension_label": label,
            "sort_order": idx,
            "question_type": "scenario",
            "min_value": 0,
            "max_value": 3,
            "options_json": _j([{"key": key, "label": text} for key, text in options]),
            "score_map_json": _j(score_map),
            "include_in_score": True,
            "required": True,
            "section_no": 1,
            "section_title": "小组协作情境判断",
        })
    return items


COGLOAD_ITEMS = [
    ("CL1", "本次小组协作学习任务对我来说具有一定难度。"),
    ("CL2", "为了完成本次小组协作任务，我需要投入较多努力。"),
    ("CL3", "理解任务要求并形成最终方案对我来说有些麻烦。"),
    ("CL4", "完成本次小组协作任务时，我感到有些挫败。"),
    ("CL5", "我觉得本次小组协作任务的时间比较紧张。"),
    ("CL6", "本次任务材料让我投入了较多心理努力。"),
    ("CL7", "达成本次协作学习目标需要我投入较多心理努力。"),
    ("CL8", "本次任务说明对我来说不太容易理解。"),
]


TAM_SCALE_ITEMS = [
    ("PEOU1", "peou", "感知易用性", "使用该平台时，我觉得操作简单。"),
    ("PEOU4", "peou", "感知易用性", "总体而言，我认为该平台易于使用。"),
    ("ATT2", "att", "使用态度", "在实验过程中，我对使用该平台的体验是愉快的。"),
    ("ATT3", "att", "使用态度", "总体而言，我对该平台持认可态度。"),
    ("BI1", "bi", "使用意愿", "如果有机会，我愿意在今后的协作学习活动中继续使用该平台。"),
    ("BI2", "bi", "使用意愿", "我愿意向其他同学推荐该平台。"),
    ("IA1", "ia", "干预适切性", "我认为系统提示通常出现在小组需要支持的时候。"),
    ("IA2", "ia", "干预适切性", "我认为系统提供的提示与当时的讨论状态是匹配的。"),
    ("IA3", "ia", "干预适切性", "我认为系统建议对调整讨论过程具有参考价值。"),
    ("NI1", "ni", "非侵入性", "我认为系统提示没有明显打断小组讨论过程。"),
    ("NI2", "ni", "非侵入性", "我认为系统支持没有给我带来明显的额外负担。"),
    ("NI3", "ni", "非侵入性", "我认为系统介入的频率是合适的。"),
    ("NI4", "ni", "非侵入性", "总体而言，我认为系统介入的方式是合适的。"),
]


def build_tam_items():
    items = [
        {
            "item_code": "DEMO1",
            "prompt_text": "您的性别：",
            "dimension_key": "demographics",
            "dimension_label": "人口学信息",
            "sort_order": 1,
            "question_type": "single_choice",
            "options_json": _j([{"key": "男", "label": "男"}, {"key": "女", "label": "女"}]),
            "include_in_score": False,
            "required": True,
            "section_no": 1,
            "section_title": "基本信息",
        },
        {
            "item_code": "DEMO2",
            "prompt_text": "您的年龄是：",
            "dimension_key": "demographics",
            "dimension_label": "人口学信息",
            "sort_order": 2,
            "question_type": "text",
            "max_value": 20,
            "include_in_score": False,
            "required": True,
            "section_no": 1,
            "section_title": "基本信息",
        },
        {
            "item_code": "DEMO3",
            "prompt_text": "您的专业是：",
            "dimension_key": "demographics",
            "dimension_label": "人口学信息",
            "sort_order": 3,
            "question_type": "text",
            "max_value": 100,
            "include_in_score": False,
            "required": True,
            "section_no": 1,
            "section_title": "基本信息",
        },
        {
            "item_code": "DEMO4",
            "prompt_text": "您的年级：",
            "dimension_key": "demographics",
            "dimension_label": "人口学信息",
            "sort_order": 4,
            "question_type": "single_choice",
            "options_json": _j([
                {"key": "本科一年级", "label": "本科一年级"},
                {"key": "本科二年级", "label": "本科二年级"},
                {"key": "本科三年级", "label": "本科三年级"},
                {"key": "本科四年级", "label": "本科四年级"},
                {"key": "硕士一年级", "label": "硕士一年级"},
                {"key": "硕士二年级", "label": "硕士二年级"},
                {"key": "硕士三年级", "label": "硕士三年级"},
                {"key": "博士一年级", "label": "博士一年级"},
                {"key": "博士二年级", "label": "博士二年级"},
                {"key": "博士三年级", "label": "博士三年级"},
                {"key": "博士四年级", "label": "博士四年级"},
                {"key": "博士五年级", "label": "博士五年级"},
            ]),
            "include_in_score": False,
            "required": True,
            "section_no": 1,
            "section_title": "基本信息",
        },
    ]
    for idx, (code, key, label, text) in enumerate(TAM_SCALE_ITEMS, start=5):
        items.append(_likert_item(code, text, key, label, idx, scale_max=5,
                                  labels=LIKERT_5_AGREE_LABELS, section_no=2,
                                  section_title="技术接受度与系统体验"))
    return items


QUESTIONNAIRES = [
    {
        "code": "ssrl_pre_v1",
        "category_key": "ssrl",
        "title": "SSRL 前测问卷",
        "description": "根据以往参与小组协作学习活动的经验作答，测量社会共享调节学习行为频率。",
        "timing": "pre",
        "scale_max": 7,
        "sort_order": 1,
        "scoring_method": "mean",
        "metadata": {"source": "docs/questionnaire/前测SSRL问卷.docx", "reverse_items": ["SSRL14", "SSRL18", "SSRL24"]},
        "items": build_ssrl_items("pre"),
    },
    {
        "code": "collab_sjt_pre_v1",
        "category_key": "collab_sjt",
        "title": "小组协作情境判断题（前测）",
        "description": "根据以往参加小组学习时的真实习惯，选择最可能采取的做法。总分范围 0-18。",
        "timing": "pre",
        "scale_max": 5,
        "sort_order": 2,
        "scoring_method": "sum",
        "metadata": {"source": "docs/questionnaire/前测小组协作情境判断题.docx"},
        "items": build_sjt_items(),
    },
    {
        "code": "emotion_interaction_pre_v1",
        "category_key": "emotion_interaction",
        "title": "情绪交互前测问卷",
        "description": "根据以往小组协作学习过程中的情绪交互行为作答。",
        "timing": "pre",
        "scale_max": 5,
        "sort_order": 3,
        "scoring_method": "mean",
        "metadata": {"source": "docs/questionnaire/前测情绪交互问卷.docx", "note": "前测源表格末行为空且部分合并单元格错位；维度按后测一致结构修正。"},
        "items": build_emotion_items("pre"),
    },
    {
        "code": "ssrl_post_v1",
        "category_key": "ssrl",
        "title": "SSRL 后测问卷",
        "description": "根据本次平台支持下完成协作学习活动的经历作答，测量社会共享调节学习行为频率。",
        "timing": "post",
        "scale_max": 7,
        "sort_order": 4,
        "scoring_method": "mean",
        "metadata": {"source": "docs/questionnaire/后测SSRL问卷.docx", "reverse_items": ["SSRL14", "SSRL18", "SSRL24"]},
        "items": build_ssrl_items("post"),
    },
    {
        "code": "emotion_interaction_post_v1",
        "category_key": "emotion_interaction",
        "title": "情绪交互后测问卷",
        "description": "根据本次小组协作学习过程中的情绪交互行为作答。",
        "timing": "post",
        "scale_max": 5,
        "sort_order": 5,
        "scoring_method": "mean",
        "metadata": {"source": "docs/questionnaire/后测情绪交互问卷.docx"},
        "items": build_emotion_items("post"),
    },
    {
        "code": "cognitive_load_post_v1",
        "category_key": "cognitive_load",
        "title": "认知负荷后测问卷",
        "description": "了解本次小组协作学习任务中的任务难度、努力、时间压力与心理负担。",
        "timing": "post",
        "scale_max": 5,
        "sort_order": 6,
        "scoring_method": "mean",
        "metadata": {"source": "docs/questionnaire/后测认知负荷问卷.docx"},
        "items": [
            _likert_item(code, text, "cognitive_load", "认知负荷", idx, scale_max=5,
                         labels=LIKERT_5_AGREE_LABELS, section_title="认知负荷")
            for idx, (code, text) in enumerate(COGLOAD_ITEMS, start=1)
        ],
    },
    {
        "code": "tam_post_v1",
        "category_key": "tam",
        "title": "基于 TAM 模型的技术接受度量表问卷",
        "description": "根据平台使用体验填写技术接受度、干预适切性和非侵入性相关题项。",
        "timing": "post",
        "scale_max": 5,
        "sort_order": 7,
        "scoring_method": "mean",
        "metadata": {"source": "docs/questionnaire/基于 TAM 模型的技术接受度量表问卷.docx"},
        "items": build_tam_items(),
    },
]


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}


def _ensure_schema():
    ensure_database_ready()
    conn = db()
    try:
        run_pending_migrations(conn)
        conn.commit()
    finally:
        conn.close()


def _upsert_questionnaire(conn, payload):
    now = now_str()
    qcols = _columns(conn, "questionnaires")
    qid_row = conn.execute("SELECT id FROM questionnaires WHERE code=?", (payload["code"],)).fetchone()
    values = {
        "code": payload["code"],
        "category_key": payload.get("category_key", "ssrl"),
        "title": payload["title"],
        "description": payload.get("description", ""),
        "timing": payload.get("timing", "both"),
        "scale_max": int(payload.get("scale_max", 5)),
        "active": 1,
        "sort_order": int(payload.get("sort_order", 0)),
        "updated_at": now,
        "is_fixed": 1,
        "version": "v1",
        "instruction_pre": payload.get("instruction_pre", ""),
        "instruction_post": payload.get("instruction_post", ""),
        "scoring_method": payload.get("scoring_method", "mean"),
        "metadata_json": _j(payload.get("metadata", {})),
    }
    if qid_row:
        update_cols = [c for c in values if c in qcols and c != "code"]
        conn.execute(
            "UPDATE questionnaires SET {} WHERE code=?".format(
                ", ".join(f"{c}=?" for c in update_cols)
            ),
            tuple(values[c] for c in update_cols) + (payload["code"],),
        )
        qid = qid_row["id"]
        action = "updated"
    else:
        values["created_at"] = now
        insert_cols = [c for c in values if c in qcols]
        conn.execute(
            "INSERT INTO questionnaires({}) VALUES({})".format(
                ", ".join(insert_cols), ", ".join("?" for _ in insert_cols)
            ),
            tuple(values[c] for c in insert_cols),
        )
        qid = conn.execute("SELECT id FROM questionnaires WHERE code=?", (payload["code"],)).fetchone()["id"]
        action = "created"

    conn.execute("DELETE FROM questionnaire_items WHERE questionnaire_id=?", (qid,))
    icols = _columns(conn, "questionnaire_items")
    for item in payload.get("items", []):
        item_values = {
            "questionnaire_id": qid,
            "item_code": item.get("item_code", ""),
            "prompt_text": item.get("prompt_text", ""),
            "dimension_label": item.get("dimension_label", ""),
            "sort_order": int(item.get("sort_order", 0)),
            "required": 1 if item.get("required", True) else 0,
            "created_at": now,
            "question_type": item.get("question_type", "likert_5"),
            "dimension_key": item.get("dimension_key", ""),
            "reverse_scored": 1 if item.get("reverse_scored", False) else 0,
            "min_value": int(item.get("min_value", 1)),
            "max_value": int(item.get("max_value", payload.get("scale_max", 5))),
            "options_json": item.get("options_json"),
            "score_map_json": item.get("score_map_json"),
            "include_in_score": 1 if item.get("include_in_score", True) else 0,
            "help_text": item.get("help_text", ""),
            "section_no": int(item.get("section_no", 1)),
            "section_title": item.get("section_title", ""),
            "scale_labels_json": item.get("scale_labels_json"),
        }
        insert_cols = [c for c in item_values if c in icols]
        conn.execute(
            "INSERT INTO questionnaire_items({}) VALUES({})".format(
                ", ".join(insert_cols), ", ".join("?" for _ in insert_cols)
            ),
            tuple(item_values[c] for c in insert_cols),
        )
    return action, qid, len(payload.get("items", []))


def main():
    _ensure_schema()
    conn = db()
    conn.row_factory = None
    try:
        conn.row_factory = __import__("sqlite3").Row
        for payload in QUESTIONNAIRES:
            action, qid, item_count = _upsert_questionnaire(conn, payload)
            print(f"[{action}] {payload['code']} id={qid} items={item_count}")
        conn.commit()
    finally:
        conn.close()
    print("Done. Formal questionnaires are active fixed questionnaires for teacher selection.")


if __name__ == "__main__":
    main()
