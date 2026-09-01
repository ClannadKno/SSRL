#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
seed_tasks.py

Seed the formal two-week collaborative learning tasks into learning_tasks and
the legacy tasks table.

Usage:
  python scripts/seed_tasks.py
  python scripts/seed_tasks.py --yes
"""

import argparse
import json
import os
import sqlite3
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from config import DB_PATH
except Exception:
    DB_PATH = os.path.join(PROJECT_ROOT, "ssrl_esp.db")


FORMAL_TASK_TIME_LIMIT_MINUTES = 80


PRE_SUBMIT_CHECKLIST = [
    "方案是否满足预算和其他硬性条件",
    "各部分内容是否相互一致",
    "是否使用了任务材料中的数据和信息",
    "是否说明了选择和取舍的理由",
    "是否提出了清晰的实施步骤",
    "是否考虑了主要风险",
    "是否提出了可以观察或测量的评价指标",
    "最终内容是否得到小组成员共同认可",
]


DEFAULT_LEARNING_TASKS = [
    {
        "title": "第一周：校园共享学习空间优化方案",
        "description": "在10万元预算内，设计兼顾个人安静学习、小组协作、数字化学习和空间管理的共享学习空间改造方案。",
        "question": "学校应如何在有限预算和场地条件下，改造一间共享学习空间，使其同时回应个人学习和小组协作需求？",
        "task_goal": "根据学生需求调查和可选建设项目，权衡预算、空间功能、噪声管理、安全和长期维护，形成全组共同认可的改造方案。",
        "output_requirement": "提交核心目标、最终项目组合及预算、选择依据与实施步骤、主要风险、3项评价指标及改进措施。",
        "keywords": ["共享学习空间", "预算权衡", "噪声管理", "空间功能", "长期维护"],
        "expected_dimensions": [
            "预算不超过10万元且至少选择3个项目",
            "同时兼顾个人安静学习和小组协作",
            "能解释学生需求数据与项目选择之间的关系",
            "包含实施步骤、风险应对和至少3项评价指标",
        ],
        "key_concepts": [
            "多目标权衡",
            "空间复合利用",
            "噪声与预约管理",
            "维护成本",
            "共同决策",
        ],
        "common_misconceptions": [
            "只按调查比例高低选择项目",
            "忽略项目组合是否超过预算",
            "只关注建设功能而忽略噪声、安全和维护",
        ],
        "acceptable_paths": [
            "优先保障安静学习与小组讨论，再用管理系统降低冲突",
            "以灵活学习区作为复合空间核心，搭配数字支持或噪声管理",
            "牺牲部分单一功能，换取更多用途转换和长期可管理性",
        ],
        "time_limit_minutes": FORMAL_TASK_TIME_LIMIT_MINUTES,
        "sort_order": 1,
        "task_type": "structured_decision",
        "task_schema_version": 2,
        "task_payload": {
            "source": "在线小组协作学习两周任务材料_参与者版.docx",
            "week": 1,
            "activity_flow": [
                {"stage": "登录与检查", "time": "5分钟", "content": "进入指定小组，确认成员和平台状态"},
                {"stage": "独立阅读", "time": "8分钟", "content": "阅读当周背景材料和任务要求"},
                {"stage": "小组讨论", "time": "30—35分钟", "content": "通过平台文字聊天交流观点"},
                {"stage": "成果整理", "time": "10—12分钟", "content": "共同填写小组方案"},
                {"stage": "检查与提交", "time": "5分钟", "content": "检查后提交最终成果"},
                {"stage": "活动后填写", "time": "5—10分钟", "content": "独立完成相应问卷"},
            ],
            "background": [
                "学校计划将一间使用率较低的公共教室改造成共享学习空间。新空间既要满足学生个人安静学习的需要，也要支持小组讨论、数字化学习和短暂交流。",
                "学校提供总预算10万元。由于场地和资金有限，小组无法建设全部项目，需要根据学生需求、空间功能、噪声管理和长期维护等因素，形成一份共同方案。",
            ],
            "task_brief": "在10万元预算内，从可选建设项目中至少选择3项，形成共享学习空间优化方案。",
            "budget": {"total": 10, "unit": "万元", "min_selected": 3},
            "survey": {
                "items": [
                    {"label": "希望增加安静学习座位", "percent": 44},
                    {"label": "希望增加小组讨论空间", "percent": 35},
                    {"label": "希望获得数字设备支持", "percent": 27},
                    {"label": "希望空间能够灵活转换用途", "percent": 22},
                    {"label": "希望增加休息和交流区域", "percent": 19},
                ],
                "note": "调查为多选结果，不能只按比例高低作出决定。",
            },
            "options": [
                {"name": "安静学习区", "cost": 4, "unit": "万元", "main_function": "提供个人安静学习座位", "concern": "空间用途较单一"},
                {"name": "小组讨论区", "cost": 3.5, "unit": "万元", "main_function": "支持多人讨论和协作", "concern": "可能产生噪声"},
                {"name": "灵活学习区", "cost": 3, "unit": "万元", "main_function": "可用于自习、讨论和展示", "concern": "管理要求较高"},
                {"name": "数字学习区", "cost": 2.5, "unit": "万元", "main_function": "提供显示、充电等设备", "concern": "后期维护成本较高"},
                {"name": "预约与噪声管理系统", "cost": 1.5, "unit": "万元", "main_function": "提高利用率并控制冲突", "concern": "不能直接增加学习空间"},
            ],
            "constraints": [
                "总费用不得超过10万元",
                "至少选择3个项目",
                "必须兼顾个人学习和小组协作",
                "必须考虑噪声、安全和长期维护",
            ],
            "discussion_questions": [
                "学习空间最需要优先解决的问题是什么？",
                "应选择哪些项目，如何进行预算和功能搭配？",
                "如何实施、控制风险并评价改造效果？",
            ],
            "submission_requirements": [
                "核心目标",
                "最终项目组合及预算",
                "选择依据与实施步骤",
                "主要风险、3项评价指标及改进措施",
            ],
            "pre_submit_checklist": PRE_SUBMIT_CHECKLIST,
        },
    },
    {
        "title": "第二周：校园学业支持服务优化方案",
        "description": "在10万元预算内，设计兼顾普遍性支持和针对性支持的校园学业支持服务组合。",
        "question": "学校应如何建设新的学业支持体系，使其在服务效果、覆盖人数、教师负担、公平性和学生参与意愿之间取得平衡？",
        "task_goal": "根据学生需求调查和可选服务项目，权衡服务覆盖、专业性、教师负担、公平性与学生隐私，形成全组共同认可的学业支持服务优化方案。",
        "output_requirement": "提交核心目标、最终服务组合及预算、选择依据与实施步骤、主要风险、3项评价指标及改进措施。",
        "keywords": ["学业支持", "普遍性支持", "针对性支持", "教师负担", "学生隐私"],
        "expected_dimensions": [
            "预算不超过10万元且至少选择3个服务项目",
            "同时包含普遍性支持和针对性支持",
            "说明各服务分别面向哪些学生需求",
            "考虑教师负担、公平性、学生隐私和参与意愿",
            "包含实施步骤、风险应对和至少3项评价指标",
        ],
        "key_concepts": [
            "服务覆盖面",
            "针对性辅导",
            "同伴互助",
            "教师工作量",
            "公平与隐私",
        ],
        "common_misconceptions": [
            "只选择专业性最强的教师服务而忽略教师负担",
            "只追求覆盖人数而缺少针对性支持",
            "忽视学生因标签化或隐私顾虑而不愿参与",
        ],
        "acceptable_paths": [
            "用在线资源和学习方法工作坊提供普遍支持，再搭配预约咨询或教师答疑提供针对性支持",
            "用同伴互助扩大持续覆盖，再设置质量控制和教师兜底机制",
            "在专业支持和服务人数之间取中间方案，并通过隐私保护提高参与意愿",
        ],
        "time_limit_minutes": FORMAL_TASK_TIME_LIMIT_MINUTES,
        "sort_order": 2,
        "task_type": "structured_decision",
        "task_schema_version": 2,
        "task_payload": {
            "source": "在线小组协作学习两周任务材料_参与者版.docx",
            "week": 2,
            "activity_flow": [
                {"stage": "登录与检查", "time": "5分钟", "content": "进入指定小组，确认成员和平台状态"},
                {"stage": "独立阅读", "time": "8分钟", "content": "阅读当周背景材料和任务要求"},
                {"stage": "小组讨论", "time": "30—35分钟", "content": "通过平台文字聊天交流观点"},
                {"stage": "成果整理", "time": "10—12分钟", "content": "共同填写小组方案"},
                {"stage": "检查与提交", "time": "5分钟", "content": "检查后提交最终成果"},
                {"stage": "活动后填写", "time": "5—10分钟", "content": "独立完成相应问卷"},
            ],
            "background": [
                "学校调查发现，学生在课程理解、学习方法、时间管理和考试准备等方面存在不同困难。目前学校主要提供教师答疑和少量学习讲座，但部分学生认为支持不够及时，服务覆盖范围也比较有限。",
                "学校提供总预算10万元，用于建设新的学业支持体系。小组需要在服务效果、覆盖人数、教师负担、公平性和学生参与意愿之间进行权衡，形成一份共同方案。",
            ],
            "task_brief": "在10万元预算内，从可选服务项目中至少选择3项，形成学业支持服务优化方案。",
            "budget": {"total": 10, "unit": "万元", "min_selected": 3},
            "survey": {
                "items": [
                    {"label": "希望获得课程重点和难点辅导", "percent": 66},
                    {"label": "希望学习更有效的学习方法", "percent": 57},
                    {"label": "希望增加同伴学习机会", "percent": 49},
                    {"label": "认为教师个别答疑时间不足", "percent": 53},
                    {"label": "担心参加辅导会被认为学习能力不足", "percent": 38},
                ],
                "note": "调查为多选结果，方案需要兼顾不同学生的需求。",
            },
            "options": [
                {"name": "教师集中答疑", "cost": 4, "unit": "万元", "main_function": "专业性强，可解决课程难点", "concern": "教师工作量较大"},
                {"name": "同伴学习互助", "cost": 3, "unit": "万元", "main_function": "支持持续交流和互相帮助", "concern": "辅导质量可能不稳定"},
                {"name": "学习方法工作坊", "cost": 2.5, "unit": "万元", "main_function": "支持时间管理、复习和笔记", "concern": "单次活动持续性有限"},
                {"name": "在线学习资源专区", "cost": 2.5, "unit": "万元", "main_function": "学生可以随时使用", "concern": "资源需要持续更新"},
                {"name": "学业困难预约咨询", "cost": 3.5, "unit": "万元", "main_function": "能够提供有针对性的支持", "concern": "服务人数有限"},
            ],
            "constraints": [
                "总费用不得超过10万元",
                "至少选择3个项目",
                "必须同时包含普遍性支持和针对性支持",
                "必须考虑教师负担、公平性和学生隐私",
            ],
            "discussion_questions": [
                "当前学业支持最需要优先解决的问题是什么？",
                "应选择哪些服务，分别面向哪些学生需求？",
                "如何实施、控制风险并评价服务效果？",
            ],
            "submission_requirements": [
                "核心目标",
                "最终服务组合及预算",
                "选择依据与实施步骤",
                "主要风险、3项评价指标及改进措施",
            ],
            "pre_submit_checklist": PRE_SUBMIT_CHECKLIST,
        },
    },
]


DEFAULT_LEGACY_TASKS = [
    {
        "title": task["title"],
        "description": task["description"],
        "is_active": 1,
    }
    for task in DEFAULT_LEARNING_TASKS
]


def _table_columns(cursor, table_name):
    return {row[1] for row in cursor.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _json_dumps(value):
    return json.dumps(value, ensure_ascii=False)


def _learning_task_row(task, now):
    return {
        "title": task["title"],
        "description": task["description"],
        "question": task["question"],
        "keywords": _json_dumps(task["keywords"]),
        "is_active": 1,
        "sort_order": int(task["sort_order"]),
        "created_at": now,
        "updated_at": now,
        "task_goal": task["task_goal"],
        "output_requirement": task["output_requirement"],
        "time_limit_minutes": int(task["time_limit_minutes"]),
        "expected_dimensions_json": _json_dumps(task["expected_dimensions"]),
        "key_concepts_json": _json_dumps(task["key_concepts"]),
        "common_misconceptions_json": _json_dumps(task["common_misconceptions"]),
        "acceptable_paths_json": _json_dumps(task["acceptable_paths"]),
        "task_type": task["task_type"],
        "experiment_phase_id": None,
        "experiment_phase_name": "",
        "agent_intervention_enabled": 1,
        "task_schema_version": int(task["task_schema_version"]),
        "task_payload_json": _json_dumps(task["task_payload"]),
    }


def task_exists(cursor, title):
    row = cursor.execute(
        "SELECT id FROM learning_tasks WHERE title=? LIMIT 1", (title,)
    ).fetchone()
    return row[0] if row else None


def seed_learning_tasks(cursor, conn):
    now = "2026-07-13 00:00:00"
    columns = _table_columns(cursor, "learning_tasks")
    count = 0
    for task in DEFAULT_LEARNING_TASKS:
        row = _learning_task_row(task, now)
        available = {k: v for k, v in row.items() if k in columns}
        existing_id = task_exists(cursor, task["title"])
        if existing_id:
            update_columns = [k for k in available.keys() if k not in {"created_at"}]
            assignments = ", ".join([f"{k}=?" for k in update_columns])
            values = [available[k] for k in update_columns] + [existing_id]
            cursor.execute(f"UPDATE learning_tasks SET {assignments} WHERE id=?", values)
            print(f"  learning_tasks: updated '{task['title']}' (id={existing_id})")
        else:
            insert_columns = list(available.keys())
            placeholders = ", ".join(["?"] * len(insert_columns))
            values = [available[k] for k in insert_columns]
            cursor.execute(
                f"INSERT INTO learning_tasks ({', '.join(insert_columns)}) VALUES ({placeholders})",
                values,
            )
            print(f"  learning_tasks: created '{task['title']}' (id={cursor.lastrowid})")
        count += 1
    conn.commit()
    return count


def seed_legacy_tasks(cursor, conn):
    now = "2026-07-13 00:00:00"
    count = 0
    for task in DEFAULT_LEGACY_TASKS:
        row = cursor.execute(
            "SELECT id FROM tasks WHERE title=? LIMIT 1", (task["title"],)
        ).fetchone()
        if row:
            cursor.execute(
                "UPDATE tasks SET description=?, is_active=? WHERE id=?",
                (task["description"], task["is_active"], row[0]),
            )
            print(f"  tasks: updated '{task['title']}' (id={row[0]})")
        else:
            cursor.execute(
                """INSERT INTO tasks
                   (title, description, is_active, created_at)
                   VALUES (?, ?, ?, ?)""",
                (task["title"], task["description"], task["is_active"], now),
            )
            print(f"  tasks: created '{task['title']}' (id={cursor.lastrowid})")
        count += 1
    conn.commit()
    return count


def seed_all_tasks(conn):
    """Seed both canonical learning_tasks and legacy tasks rows."""
    cursor = conn.cursor()
    print("learning_tasks ...")
    learning_count = seed_learning_tasks(cursor, conn)
    print("tasks (legacy) ...")
    legacy_count = seed_legacy_tasks(cursor, conn)
    return learning_count, legacy_count


def main():
    parser = argparse.ArgumentParser(description="播种两周协作学习任务")
    parser.add_argument("--yes", "-y", action="store_true", help="跳过确认")
    args = parser.parse_args()

    if not args.yes:
        ans = input("播种两周协作学习任务到数据库？[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("已取消。")
            sys.exit(0)

    os.chdir(PROJECT_ROOT)

    if not os.path.isfile(DB_PATH):
        print(f"错误：未找到数据库 {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)

    try:
        seed_all_tasks(conn)
        print("完成。可通过以下方式验证：")
        print(
            "  python -c \"import sqlite3; c=sqlite3.connect('{}'); "
            "print(c.execute('SELECT id,title,is_active,sort_order FROM learning_tasks ORDER BY sort_order,id').fetchall()); "
            "print(c.execute('SELECT id,title,is_active FROM tasks ORDER BY id').fetchall()); c.close()\"".format(
                os.path.basename(DB_PATH)
            )
        )
    except Exception as e:
        conn.rollback()
        print(f"\n错误：{e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
