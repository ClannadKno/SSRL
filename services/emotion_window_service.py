# -*- coding: utf-8 -*-
"""Frozen, discussion-scoped input windows for the emotion Agent.

The scheduler calls this module while holding its slot reservation transaction.
Both equal-length windows, the exact student-message snapshots, and their
deterministic metrics are therefore immutable for the lifetime of a slot.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from datetime import timedelta
from typing import Iterable, Optional

from db import db, parse_dt


EMOTION_SLOT_PROMPT_VERSION = "emotion_slot_windows_v1"

_ACKNOWLEDGEMENTS = {
    "好",
    "好的",
    "行",
    "可以",
    "同意",
    "收到",
    "嗯",
    "哦",
    "对",
    "是",
    "ok",
    "okay",
    "yes",
    "+1",
}
_TEST_OR_CHECKIN = {
    "测试",
    "test",
    "签到",
    "打卡",
    "已签到",
    "测试一下",
}


def _fmt(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _loads(value, fallback):
    if value in (None, ""):
        return fallback
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return parsed


class EmotionWindowService:
    """Build and load the immutable two-window snapshot for one emotion slot."""

    @staticmethod
    def bounds(started_at, slot_index: int, interval_seconds: int) -> dict:
        started = parse_dt(started_at) if isinstance(started_at, str) else started_at
        if not started:
            raise ValueError("discussion started_at is required")
        slot_index = int(slot_index)
        interval_seconds = max(1, int(interval_seconds))
        if slot_index < 1:
            raise ValueError("slot_index must be positive")

        current_end = started + timedelta(seconds=interval_seconds * slot_index)
        current_start = current_end - timedelta(seconds=interval_seconds)
        previous_end = current_start
        previous_start = previous_end - timedelta(seconds=interval_seconds)
        return {
            "previous_window_start": _fmt(previous_start),
            "previous_window_end": _fmt(previous_end),
            "current_window_start": _fmt(current_start),
            "current_window_end": _fmt(current_end),
            "window_seconds": interval_seconds,
        }

    @staticmethod
    def _normalise_content(value) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip()).lower()

    @classmethod
    def _low_information_reason(cls, content: str) -> Optional[str]:
        normalised = cls._normalise_content(content)
        if not normalised:
            return "empty"
        if normalised in _ACKNOWLEDGEMENTS:
            return "short_acknowledgement"
        if normalised in _TEST_OR_CHECKIN:
            return "test_or_checkin"
        meaningful = False
        for char in normalised:
            category = unicodedata.category(char)
            if char.isalnum() or category.startswith("L"):
                meaningful = True
                break
        if not meaningful:
            return "symbol_or_punctuation_only"
        return None

    @classmethod
    def _annotate_messages(cls, rows: Iterable) -> list:
        messages = []
        seen_content = set()
        for row in rows:
            item = dict(row)
            normalised = cls._normalise_content(item.get("content"))
            reason = cls._low_information_reason(item.get("content"))
            if normalised and normalised in seen_content:
                reason = reason or "duplicate_content"
            if normalised:
                seen_content.add(normalised)
            item["low_information_message"] = bool(reason)
            item["low_information_reason"] = reason
            messages.append(item)
        return messages

    @staticmethod
    def _query_window(
        conn,
        *,
        group_id: int,
        session_id: int,
        discussion_id: int,
        window_start: str,
        window_end: str,
    ) -> list:
        rows = conn.execute(
            """
            SELECT m.id, m.sequence, m.user_id, m.content, m.created_at,
                   m.reply_to_message_id,
                   COALESCE((
                       SELECT '成员' || ep.member_no
                       FROM experiment_participants AS ep
                       WHERE ep.group_id=m.group_id AND ep.user_id=m.user_id
                       ORDER BY ep.is_active DESC, ep.id DESC
                       LIMIT 1
                   ), '成员') AS member_label
            FROM messages AS m
            WHERE m.group_id=? AND m.session_id=? AND m.discussion_id=?
              AND m.role='student'
              AND m.created_at>=? AND m.created_at<?
            ORDER BY m.created_at ASC, m.sequence ASC, m.id ASC
            """,
            (
                int(group_id),
                int(session_id),
                int(discussion_id),
                window_start,
                window_end,
            ),
        ).fetchall()
        return EmotionWindowService._annotate_messages(rows)

    @staticmethod
    def metrics(messages: list) -> dict:
        messages = list(messages or [])
        effective = [
            item for item in messages if not item.get("low_information_message")
        ]
        member_counts = Counter(item.get("user_id") for item in messages)
        reply_or_response_count = 0
        previous_effective_user = None
        for item in effective:
            user_id = item.get("user_id")
            if item.get("reply_to_message_id") is not None or (
                previous_effective_user is not None
                and user_id != previous_effective_user
            ):
                reply_or_response_count += 1
            previous_effective_user = user_id

        active_minutes = {
            str(item.get("created_at") or "")[:16]
            for item in messages
            if item.get("created_at")
        }
        message_count = len(messages)
        max_member_message_ratio = (
            round(max(member_counts.values()) / message_count, 4)
            if message_count and member_counts
            else 0.0
        )
        return {
            "message_count": message_count,
            "effective_message_count": len(effective),
            "active_member_count": len(
                {item.get("user_id") for item in messages if item.get("user_id") is not None}
            ),
            "effective_char_count": sum(
                len(str(item.get("content") or "").strip()) for item in effective
            ),
            "reply_or_response_count": reply_or_response_count,
            # Local preparation only removes deterministically low-information
            # messages; semantic task relevance remains an LLM responsibility.
            "task_related_message_count": len(effective),
            "short_acknowledgement_count": sum(
                1
                for item in messages
                if item.get("low_information_reason") == "short_acknowledgement"
            ),
            "low_information_message_count": sum(
                1 for item in messages if item.get("low_information_message")
            ),
            "active_minutes": len(active_minutes),
            "max_member_message_ratio": max_member_message_ratio,
        }

    @classmethod
    def freeze_slot(
        cls,
        conn,
        *,
        slot_id: int,
        group_id: int,
        session_id: int,
        discussion_id: int,
        slot_index: int,
        started_at,
        interval_seconds: int,
        prompt_version: str = EMOTION_SLOT_PROMPT_VERSION,
        frozen_at: Optional[str] = None,
    ) -> dict:
        row = conn.execute(
            "SELECT * FROM emotion_reflection_slots WHERE id=?", (int(slot_id),)
        ).fetchone()
        if not row:
            raise ValueError("emotion slot not found")
        if row["window_frozen_at"]:
            return cls.snapshot_from_slot(dict(row))

        bounds = cls.bounds(started_at, slot_index, interval_seconds)
        previous = cls._query_window(
            conn,
            group_id=group_id,
            session_id=session_id,
            discussion_id=discussion_id,
            window_start=bounds["previous_window_start"],
            window_end=bounds["previous_window_end"],
        )
        current = cls._query_window(
            conn,
            group_id=group_id,
            session_id=session_id,
            discussion_id=discussion_id,
            window_start=bounds["current_window_start"],
            window_end=bounds["current_window_end"],
        )
        previous_metrics = cls.metrics(previous)
        current_metrics = cls.metrics(current)
        previous_ids = [int(item["id"]) for item in previous]
        current_ids = [int(item["id"]) for item in current]
        input_ids = previous_ids + current_ids
        frozen_at = frozen_at or bounds["current_window_end"]

        conn.execute(
            """
            UPDATE emotion_reflection_slots
            SET prompt_version=?,
                previous_window_start=?, previous_window_end=?,
                current_window_start=?, current_window_end=?,
                previous_metrics_json=?, current_metrics_json=?,
                previous_message_ids_json=?, current_message_ids_json=?,
                input_message_ids_json=?,
                previous_messages_json=?, current_messages_json=?,
                window_frozen_at=?, updated_at=?
            WHERE id=? AND window_frozen_at IS NULL
            """,
            (
                str(prompt_version),
                bounds["previous_window_start"],
                bounds["previous_window_end"],
                bounds["current_window_start"],
                bounds["current_window_end"],
                json.dumps(previous_metrics, ensure_ascii=False),
                json.dumps(current_metrics, ensure_ascii=False),
                json.dumps(previous_ids, ensure_ascii=False),
                json.dumps(current_ids, ensure_ascii=False),
                json.dumps(input_ids, ensure_ascii=False),
                json.dumps(previous, ensure_ascii=False),
                json.dumps(current, ensure_ascii=False),
                frozen_at,
                frozen_at,
                int(slot_id),
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO emotion_feedback_assessments(
                slot_id, group_id, session_id, discussion_id, slot_index,
                prompt_version, status,
                previous_metrics_json, current_metrics_json,
                input_message_ids_json, evidence_message_ids_json,
                created_at, updated_at
            ) VALUES(?,?,?,?,?,?,'prepared',?,?,?,?,?,?)
            """,
            (
                int(slot_id),
                int(group_id),
                int(session_id),
                int(discussion_id),
                int(slot_index),
                str(prompt_version),
                json.dumps(previous_metrics, ensure_ascii=False),
                json.dumps(current_metrics, ensure_ascii=False),
                json.dumps(input_ids, ensure_ascii=False),
                json.dumps([], ensure_ascii=False),
                frozen_at,
                frozen_at,
            ),
        )
        final = conn.execute(
            "SELECT * FROM emotion_reflection_slots WHERE id=?", (int(slot_id),)
        ).fetchone()
        return cls.snapshot_from_slot(dict(final))

    @classmethod
    def ensure_slot_snapshot(cls, slot_id: int, *, frozen_at: Optional[str] = None) -> dict:
        conn = db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT ers.*, gsd.started_at AS discussion_started_at
                FROM emotion_reflection_slots AS ers
                JOIN group_session_discussions AS gsd ON gsd.id=ers.discussion_id
                WHERE ers.id=?
                """,
                (int(slot_id),),
            ).fetchone()
            if not row:
                raise ValueError("emotion slot not found")
            if row["window_frozen_at"]:
                snapshot = cls.snapshot_from_slot(dict(row))
            else:
                scheduled = parse_dt(row["scheduled_at"])
                started = parse_dt(row["discussion_started_at"])
                if not scheduled or not started:
                    raise ValueError("emotion slot has invalid schedule")
                interval_seconds = max(1, int((scheduled - started).total_seconds()) // int(row["slot_index"]))
                snapshot = cls.freeze_slot(
                    conn,
                    slot_id=int(row["id"]),
                    group_id=int(row["group_id"]),
                    session_id=int(row["session_id"]),
                    discussion_id=int(row["discussion_id"]),
                    slot_index=int(row["slot_index"]),
                    started_at=started,
                    interval_seconds=interval_seconds,
                    prompt_version=row["prompt_version"] or EMOTION_SLOT_PROMPT_VERSION,
                    frozen_at=frozen_at,
                )
            conn.commit()
            return snapshot
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def snapshot_from_slot(slot: dict) -> dict:
        previous_messages = _loads(slot.get("previous_messages_json"), [])
        current_messages = _loads(slot.get("current_messages_json"), [])
        return {
            "slot_id": int(slot["id"]),
            "slot_index": int(slot["slot_index"]),
            "prompt_version": slot.get("prompt_version") or EMOTION_SLOT_PROMPT_VERSION,
            "previous_window_start": slot.get("previous_window_start"),
            "previous_window_end": slot.get("previous_window_end"),
            "current_window_start": slot.get("current_window_start"),
            "current_window_end": slot.get("current_window_end"),
            "window_frozen_at": slot.get("window_frozen_at"),
            "previous_messages": previous_messages,
            "current_messages": current_messages,
            "previous_message_ids": _loads(slot.get("previous_message_ids_json"), []),
            "current_message_ids": _loads(slot.get("current_message_ids_json"), []),
            "input_message_ids": _loads(slot.get("input_message_ids_json"), []),
            "previous_metrics": _loads(slot.get("previous_metrics_json"), {}),
            "current_metrics": _loads(slot.get("current_metrics_json"), {}),
        }


__all__ = [
    "EMOTION_SLOT_PROMPT_VERSION",
    "EmotionWindowService",
]
