# -*- coding: utf-8 -*-
"""Batch 2: 预创建实验组、学生身份、学生密钥与教师密钥。

用法：
    python scripts/generate_experiment_keys.py
    python scripts/generate_experiment_keys.py --force-rotate
    python scripts/generate_experiment_keys.py --groups 15 --members-per-group 4 --output generated_experiment_keys.csv

该脚本幂等且安全，重复执行不会产生重复数据。
"""

import argparse
import csv
import os
import random
import string
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
# 常量
# ––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
# 用于随机后缀的安全字符集，排除易混淆的 O/0/I/1
_SAFE_CHARS = (
    string.ascii_uppercase.replace("O", "").replace("I", "")
    + string.digits.replace("0", "").replace("1", "")
)
_SUFFIX_LENGTH = 5


def _generate_suffix(length=_SUFFIX_LENGTH):
    """生成一个安全的随机后缀（大写字母 + 数字，不含 O/0/I/1）。"""
    return "".join(random.choices(_SAFE_CHARS, k=length))


def _make_hash(password):
    """使用 werkzeug 安全哈希明文密钥。"""
    from werkzeug.security import generate_password_hash
    return generate_password_hash(password)


# ––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––
# 数据库辅助函数
# ––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––––

def _ensure_tables():
    """确保核心表与实验身份表存在（幂等）。"""
    from db import ensure_database_ready
    ensure_database_ready()
    from db import db, ensure_experiment_identity_tables
    conn = db()
    try:
        ensure_experiment_identity_tables(conn)
        conn.commit()
    finally:
        conn.close()


def _get_group_ids(conn, group_count, force_rotate):
    """获取或创建 G01–G15 组，返回按顺序排列的 group_id 列表。

    - 已存在的组会被复用（幂等）。
    - force_rotate 时先删除旧组及相关记录。
    - 检测到重复 group_code 时输出 warning。
    """
    existing = {}
    for row in conn.execute(
        "SELECT id, group_code FROM groups WHERE group_code IS NOT NULL AND group_code != '' ORDER BY id DESC"
    ).fetchall():
        existing[row["group_code"]] = row["id"]
    # Count duplicates per group_code and warn
    seen = {}
    for row in conn.execute(
        "SELECT id, group_code FROM groups WHERE group_code IS NOT NULL AND group_code != '' ORDER BY id DESC"
    ).fetchall():
        try:
            seen[row["group_code"]] = seen.get(row["group_code"], 0) + 1
        except Exception:
            pass
    for gcode, cnt in seen.items():
        if cnt > 1:
            print(f"[WARNING] Found {cnt} duplicate group_code={gcode}, reusing latest group(id={existing[gcode]})")

    if force_rotate:
        gids_to_delete = []
        for i in range(1, group_count + 1):
            gcode = "G{:02d}".format(i)
            if gcode in existing:
                gids_to_delete.append(existing[gcode])
        for gid in gids_to_delete:
            conn.execute("DELETE FROM experiment_participants WHERE group_id=?", (gid,))
            conn.execute("DELETE FROM group_members WHERE group_id=?", (gid,))
        existing = {}

    group_ids = []
    for i in range(1, group_count + 1):
        gcode = "G{:02d}".format(i)
        if gcode in existing:
            group_ids.append(existing[gcode])
            continue
        name = "第{:02d}组".format(i)
        from db import now_str
        conn.execute(
            "INSERT INTO groups(name, group_code, condition, state, created_at) VALUES(?,?,?,?,?)",
            (name, gcode, "experiment", "OPEN", now_str()),
        )
        gid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        group_ids.append(gid)

    return group_ids


