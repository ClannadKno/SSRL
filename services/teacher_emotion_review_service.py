# -*- coding: utf-8 -*-
"""Teacher-facing collaboration review packet for the T4 emotion trend page.

This service is read-only. It combines normalized state segments, chat
messages, participation counts, and intervention logs into one timeline-shaped
payload for the teacher UI. Student message states come only from explicit
message_range segments; unmatched messages use assessment progress to
distinguish completed-without-a-state from still waiting for detection.
"""
from __future__ import annotations

from datetime import timedelta
import json

from db import now_str, query_all, query_one
from services.teacher_emotion_trend_service import (
    CANONICAL_STATE_SOURCE_POLICY,
    FORMAL_STATE_ORDER,
    OBSERVING_STATE,
    STATE_LABEL_MAP,
    UNCLASSIFIED_STATE,
    _map_state_code,
    _map_state_label,
    _parse_dt,
    _resolve_time_range,
    _session_scope,
    get_emotion_trend,
)
from services.message_state_assignment_service import (
    MESSAGE_STATE_ASSIGNMENT_POLICY,
    assign_message_states,
)

_SEGMENT_SOURCE_PRIORITY = {
    "session_finalizer": 3,
    "state_monitor": 3,
    "strategy_llm": 2,
    "silence_rule": 1,
}


def _safe_json(value):
    if not value:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _norm_role(row):
    role = (row.get("role") or row.get("sender_type") or row.get("user_role") or "").strip().lower()
    if role == "agent":
        return "agent"
    if role == "teacher":
        return "teacher"
    return "student"


def _display_name(row, role):
    if role == "agent":
        agent_type = (row.get("agent_type") or "").strip().lower()
        if agent_type == "emotion":
            return "情绪智能体"
        if agent_type == "strategy":
            return "策略智能体"
        return "Agent"
    return (
        row.get("participant_code")
        or row.get("real_name")
        or row.get("username")
        or ("用户 %s" % row.get("user_id"))
    )


def _row_to_message(row):
    data = dict(row)
    role = _norm_role(data)
    agent_kind = _intervention_kind(data) if role == "agent" else None
    return {
        "id": data.get("id"),
        "sequence": data.get("sequence") or data.get("id"),
        "created_at": data.get("created_at"),
        "role": role,
        "sender_type": data.get("sender_type") or role,
        "user_id": data.get("user_id"),
        "display_name": _display_name(data, role),
        "participant_code": data.get("participant_code"),
        "content": data.get("content") or "",
        "linked_log_id": data.get("linked_log_id"),
        "intervention_run_id": data.get("intervention_run_id"),
        "agent_type": data.get("agent_type"),
        "strategy_id": data.get("strategy_id"),
        "session_id": data.get("session_id"),
        "session_no": data.get("session_no"),
        "task_id": data.get("task_id"),
        "metadata": _safe_json(data.get("metadata_json")),
        "agent_message_kind": agent_kind,
        "agent_trigger_source": data.get("trigger_source"),
        "help_request_id": data.get("help_request_id"),
        "agent_display_label": _agent_label(agent_kind) if agent_kind else None,
    }


def _message_query(
    group_id, session_id, scope, ws, we, *, include_legacy_scope=False
):
    params = [group_id]
    session_filter = ""
    if session_id:
        if include_legacy_scope:
            session_filter = (
                "AND (m.session_id=? OR "
                "(m.session_id IS NULL AND (? IS NULL OR m.session_no=?)))"
            )
            params.extend(
                [session_id, scope.get("session_no"), scope.get("session_no")]
            )
        else:
            session_filter = "AND m.session_id=?"
            params.append(session_id)
    params.extend([ws, we])
    return query_all(
        """
        SELECT m.id, m.group_id, m.user_id, m.content, m.sequence,
               m.sender_type, m.role, m.session_no, m.task_id,
               m.linked_log_id, m.intervention_run_id, m.metadata_json,
               m.created_at, m.session_id, m.agent_type, m.strategy_id,
               COALESCE(m.trigger_source, il.trigger_source, ir.trigger_type) AS trigger_source,
               il.push_mode, il.pushed_by_user_id,
               COALESCE(il.help_request_id, ir.help_request_id) AS help_request_id,
               u.real_name, u.username, u.participant_code, u.role AS user_role
          FROM messages m
          LEFT JOIN users u ON u.id = m.user_id
          LEFT JOIN intervention_logs il ON il.id = m.linked_log_id
          LEFT JOIN intervention_runs ir
            ON ir.id = m.intervention_run_id
            OR (il.intervention_id IS NOT NULL AND ir.id = il.intervention_id)
         WHERE m.group_id=?
           {session_filter}
           AND m.created_at>=?
           AND m.created_at<=?
         ORDER BY COALESCE(m.sequence, m.id), m.id
        """.format(session_filter=session_filter),
        tuple(params),
    )


def _fetch_messages(
    group_id, session_id, scope, ws, we, *, include_legacy_scope=False
):
    rows = _message_query(
        group_id,
        session_id,
        scope,
        ws,
        we,
        include_legacy_scope=include_legacy_scope,
    )
    return [_row_to_message(r) for r in rows]


def _dt(value):
    if not value:
        return None
    return _parse_dt(str(value).replace("T", " ")[:19])


def _segment_priority(segment):
    return (
        1 if segment.get("is_finalized") else 0,
        _SEGMENT_SOURCE_PRIORITY.get(segment.get("source"), 0),
        str(segment.get("updated_at") or ""),
        int(segment.get("id") or 0),
    )


def _assign_state_to_messages(
    messages,
    state_segments,
    display_context=None,
    silence_segments=None,
    *,
    group_id=None,
    session_id=None,
    discussion_id=None,
):
    display_context = display_context or {
        "state_source_mode": "canonical",
        "state_source_policy": CANONICAL_STATE_SOURCE_POLICY,
    }
    assignment_payload = assign_message_states(
        messages=messages,
        state_segments=state_segments,
        display_context=display_context,
        silence_segments=silence_segments or [],
        group_id=group_id,
        session_id=session_id,
        discussion_id=discussion_id or display_context.get("discussion_id"),
    )
    by_sequence = assignment_payload["by_sequence"]
    by_message_id = assignment_payload["by_message_id"]
    for message in messages:
        role = message.get("role")
        assignment = None
        if message.get("sequence") is not None:
            try:
                assignment = by_sequence.get(int(message.get("sequence")))
            except (TypeError, ValueError):
                assignment = None
        if assignment is None and message.get("id") is not None:
            assignment = by_message_id.get(message.get("id"))
        if role != "student":
            message["semantic_state"] = None
            message["assessment_status"] = None
            message["state_code"] = None
            message["state_label"] = None
            message["final_sub_state_code"] = None
            message["final_sub_state_label"] = None
            message["display_state_code"] = None
            message["display_state_label"] = None
            message["coarse_state_code"] = None
            message["legacy_state_code"] = None
            message["state_overlays"] = []
            message["assignment_source"] = (
                assignment.get("assignment_source")
                if assignment
                else "non_student_message"
            )
            message["inferred"] = False
            message["state_source"] = None
            message["state_confidence"] = None
            message["state_window_start"] = None
            message["state_window_end"] = None
            message["state_segment_id"] = None
            message["source_segment_id"] = None
            message["source_batch_id"] = None
            message["error_code"] = None
            message["state_evidence_sequences"] = []
            message["state_evidence_message_ids"] = []
            message["canonical_segment_id"] = None
            message["segment_source"] = None
            message["state_assignment_reason"] = None
            message["context_state_code"] = (
                assignment.get("context_state_code") if assignment else None
            )
            continue

        assigned = assignment.get("segment") if assignment else None
        assessment_status = (
            assignment.get("assessment_status") if assignment else UNCLASSIFIED_STATE
        )
        semantic_state = assignment.get("final_sub_state_code") if assignment else None
        message["semantic_state"] = semantic_state
        message["assessment_status"] = assessment_status
        message["final_sub_state_code"] = semantic_state
        message["final_sub_state_label"] = (
            assignment.get("final_sub_state_label") if assignment else None
        )
        message["display_state_code"] = (
            assignment.get("display_state_code")
            if assignment
            else UNCLASSIFIED_STATE
        )
        message["display_state_label"] = (
            assignment.get("display_state_label")
            if assignment
            else STATE_LABEL_MAP[UNCLASSIFIED_STATE]
        )
        message["coarse_state_code"] = (
            assignment.get("coarse_state_code") if assignment else None
        )
        message["legacy_state_code"] = (
            assignment.get("legacy_state_code") if assignment else None
        )
        message["state_overlays"] = (
            assignment.get("state_overlays") if assignment else []
        )
        message["assignment_source"] = (
            assignment.get("assignment_source") if assignment else "unclassified"
        )
        message["inferred"] = bool(assignment.get("inferred")) if assignment else False
        message["source_segment_id"] = (
            assignment.get("source_segment_id") if assignment else None
        )
        message["source_batch_id"] = (
            assignment.get("source_batch_id") if assignment else None
        )
        message["error_code"] = assignment.get("error_code") if assignment else None
        message["context_state_code"] = None
        if assigned:
            message["state_code"] = message["display_state_code"]
            message["state_label"] = message["display_state_label"]
            message["state_source"] = assignment.get("assignment_source")
            message["state_confidence"] = assigned.get("confidence")
            message["state_window_start"] = assigned.get("start_at")
            message["state_window_end"] = assigned.get("end_at")
            message["state_segment_id"] = assignment.get("source_segment_id")
            message["canonical_segment_id"] = assigned.get(
                "canonical_segment_id",
                assigned.get("id"),
            )
            message["segment_source"] = assigned.get(
                "segment_source",
                assigned.get("source"),
            )
            message["state_assignment_reason"] = assignment.get(
                "state_assignment_reason"
            )
            evidence = (
                assigned.get("evidence_sequences")
                or assigned.get("evidence_message_ids")
                or []
            )
            message["state_evidence_sequences"] = evidence
            message["state_evidence_message_ids"] = evidence
        else:
            message["state_code"] = message["display_state_code"]
            message["state_label"] = message["display_state_label"]
            message["state_source"] = message["assignment_source"]
            message["state_confidence"] = None
            message["state_window_start"] = None
            message["state_window_end"] = None
            message["state_segment_id"] = None
            message["canonical_segment_id"] = None
            message["segment_source"] = None
            message["state_assignment_reason"] = assignment.get(
                "state_assignment_reason"
            ) if assignment else "unclassified"
            message["state_evidence_sequences"] = []
            message["state_evidence_message_ids"] = []
    return assignment_payload


def _snapshot_sequences(snapshot, messages):
    if snapshot.get("first_sequence") is not None or snapshot.get("last_sequence") is not None:
        return {
            "first_sequence": snapshot.get("first_sequence"),
            "last_sequence": snapshot.get("last_sequence"),
        }
    start = _dt(snapshot.get("window_start"))
    end = _dt(snapshot.get("window_end")) or start
    seqs = []
    if start and end:
        for message in messages:
            if message.get("role") != "student":
                continue
            mdt = _dt(message.get("created_at"))
            if mdt and start <= mdt <= end:
                seqs.append(message.get("sequence"))
    seqs = [s for s in seqs if s is not None]
    return {
        "first_sequence": min(seqs) if seqs else None,
        "last_sequence": max(seqs) if seqs else None,
    }


def _state_runs(snapshots):
    runs = []
    for snap in sorted(snapshots or [], key=lambda s: s.get("window_start") or ""):
        state = _map_state_code(snap.get("collaboration_state"))
        if state == OBSERVING_STATE:
            continue
        label = snap.get("state_label") or _map_state_label(state)
        last = runs[-1] if runs else None
        if last and last["state_code"] == state:
            last["window_end"] = snap.get("window_end") or snap.get("window_start")
            last["count"] += 1
            last["avg_confidence"] = round(
                ((last["avg_confidence"] * (last["count"] - 1)) + float(snap.get("confidence") or 0.0))
                / last["count"],
                4,
            )
        else:
            runs.append({
                "state_code": state,
                "state_label": label,
                "window_start": snap.get("window_start"),
                "window_end": snap.get("window_end") or snap.get("window_start"),
                "count": 1,
                "avg_confidence": round(float(snap.get("confidence") or 0.0), 4),
            })
    return runs


def _intervention_kind(record):
    agent_type = str(record.get("agent_type") or "").strip().lower()
    source = str(record.get("trigger_source") or "").strip().lower()
    mode = str(record.get("push_mode") or "").strip().lower()
    strategy_id = record.get("strategy_id")
    help_request_id = record.get("help_request_id")
    if agent_type == "emotion":
        return "emotion"
    if help_request_id or "student_help" in source or mode == "student_request":
        return "strategy_student_help"
    if source in {"auto", "auto_state", "auto_intervention", "new_message", "state_monitor", "monitor_run", "auto_v2", "sera_auto", "sera_auto_v2"} or source.startswith("auto") or mode.startswith("auto") or mode.startswith("sera_auto"):
        return "strategy_auto"
    if "teacher" in source or mode in ("sera_teacher_confirmed", "teacher", "teacher_confirmed"):
        return "strategy_teacher"
    if (
        source == "legacy_unknown"
        or agent_type == "strategy"
        or strategy_id
        or record.get("intervention_id")
        or record.get("intervention_run_id")
        or record.get("linked_log_id")
    ):
        return "legacy_agent"
    if record.get("pushed_by_user_id"):
        return "strategy_teacher"
    return "legacy_agent"


def _agent_label(kind):
    return {
        "emotion": "情绪智能体",
        "strategy_auto": "策略智能体 · 自动介入",
        "strategy_student_help": "策略智能体 · 学生求助",
        "strategy_teacher": "教师介入",
        "legacy_agent": "Agent · legacy/未知来源",
    }.get(kind, "Agent · legacy/未知来源")


def _actor_for_kind(kind):
    return {
        "emotion": "emotion_agent",
        "strategy_auto": "strategy_agent",
        "strategy_student_help": "strategy_agent",
        "strategy_teacher": "teacher",
        "legacy_agent": "agent",
    }.get(kind, "agent")


def _annotate_agent_messages(messages, interventions):
    by_message_id = {}
    by_sequence = {}
    for item in interventions or []:
        if item.get("linked_message_id") is not None:
            by_message_id[item["linked_message_id"]] = item
        if item.get("linked_sequence") is not None:
            by_sequence[item["linked_sequence"]] = item

    for message in messages:
        if message.get("role") != "agent":
            continue
        linked = by_message_id.get(message.get("id")) or by_sequence.get(message.get("sequence"))
        kind = linked.get("intervention_kind") if linked else _intervention_kind(message)
        message["agent_message_kind"] = kind
        message["agent_trigger_source"] = linked.get("trigger_type") if linked else None
        message["agent_display_label"] = _agent_label(kind)


def _participation(messages):
    buckets = {}
    for msg in messages:
        if msg.get("role") != "student":
            continue
        key = msg.get("display_name") or ("用户 %s" % msg.get("user_id"))
        item = buckets.setdefault(key, {
            "display_name": key,
            "user_id": msg.get("user_id"),
            "message_count": 0,
            "char_count": 0,
            "first_at": msg.get("created_at"),
            "last_at": msg.get("created_at"),
        })
        item["message_count"] += 1
        item["char_count"] += len(msg.get("content") or "")
        item["last_at"] = msg.get("created_at")
    return sorted(buckets.values(), key=lambda item: (-item["message_count"], item["display_name"]))


def _coerce_bucket_minutes(value):
    try:
        return max(1, int(value or 1))
    except (TypeError, ValueError):
        return 1


def _floor_bucket(dt, bucket_minutes):
    minute = (dt.minute // bucket_minutes) * bucket_minutes
    return dt.replace(minute=minute, second=0, microsecond=0)


def _participation_timeline(messages, bucket_minutes=1):
    bucket_minutes = _coerce_bucket_minutes(bucket_minutes)
    timed = []
    for message in messages or []:
        mdt = _dt(message.get("created_at"))
        if mdt:
            timed.append((mdt, message))
    if not timed:
        return []

    participants = _participation(messages)
    names = [item["display_name"] for item in participants]
    start = _floor_bucket(min(item[0] for item in timed), bucket_minutes)
    end = _floor_bucket(max(item[0] for item in timed), bucket_minutes)
    bucket_delta = timedelta(minutes=bucket_minutes)

    buckets = []
    current = start
    while current <= end:
        buckets.append({
            "bucket_start": current.strftime("%Y-%m-%d %H:%M:%S"),
            "bucket_end": (current + bucket_delta).strftime("%Y-%m-%d %H:%M:%S"),
            "message_count": 0,
            "student_message_count": 0,
            "agent_message_count": 0,
            "active_student_count": 0,
            "first_sequence": None,
            "last_sequence": None,
            "_students": {
                name: {
                    "display_name": name,
                    "message_count": 0,
                    "char_count": 0,
                }
                for name in names
            },
            "_active": set(),
        })
        current += bucket_delta

    for mdt, message in timed:
        index = int(((_floor_bucket(mdt, bucket_minutes) - start).total_seconds()) // bucket_delta.total_seconds())
        if index < 0 or index >= len(buckets):
            continue
        bucket = buckets[index]
        seq = message.get("sequence")
        bucket["message_count"] += 1
        if seq is not None:
            bucket["first_sequence"] = seq if bucket["first_sequence"] is None else min(bucket["first_sequence"], seq)
            bucket["last_sequence"] = seq if bucket["last_sequence"] is None else max(bucket["last_sequence"], seq)
        if message.get("role") == "agent":
            bucket["agent_message_count"] += 1
            continue
        if message.get("role") != "student":
            continue
        name = message.get("display_name") or ("用户 %s" % message.get("user_id"))
        if name not in bucket["_students"]:
            bucket["_students"][name] = {
                "display_name": name,
                "message_count": 0,
                "char_count": 0,
            }
        item = bucket["_students"][name]
        item["message_count"] += 1
        item["char_count"] += len(message.get("content") or "")
        bucket["student_message_count"] += 1
        if message.get("user_id"):
            bucket["_active"].add(message.get("user_id"))

    result = []
    for bucket in buckets:
        students = [
            bucket["_students"].get(name, {
                "display_name": name,
                "message_count": 0,
                "char_count": 0,
            })
            for name in names
        ]
        extra_names = [
            name for name in sorted(bucket["_students"])
            if name not in names
        ]
        students.extend(bucket["_students"][name] for name in extra_names)
        bucket["students"] = students
        bucket["active_student_count"] = len(bucket["_active"])
        del bucket["_students"]
        del bucket["_active"]
        result.append(bucket)
    return result


def _fetch_interventions(
    group_id,
    session_id,
    scope,
    ws,
    we,
    messages,
    *,
    include_legacy_scope=False,
):
    params = [group_id]
    session_filter = ""
    if session_id:
        if include_legacy_scope:
            session_filter = (
                "AND (il.session_id=? OR "
                "(il.session_id IS NULL AND ? IS NOT NULL AND il.task_id=?))"
            )
            params.extend(
                [session_id, scope.get("task_id"), scope.get("task_id")]
            )
        else:
            session_filter = "AND il.session_id=?"
            params.append(session_id)
    params.extend([ws, we])
    rows = query_all(
        """
        SELECT il.*, ir.detected_state, ir.confidence AS run_confidence,
               ir.status AS run_status, ir.cutoff_sequence,
               ir.target_segment_id,
               COALESCE(ir.state_assessment_id, il.state_assessment_id)
                   AS target_state_assessment_id,
               ir.generated_at, ir.published_at, ir.failure_reason,
               ir.trigger_type AS run_trigger_type,
               ir.agent_type AS run_agent_type,
               ir.help_request_id AS run_help_request_id,
               iu.auto_uptake_type, iu.manual_uptake_type
          FROM intervention_logs il
          LEFT JOIN intervention_runs ir
            ON ir.id = il.intervention_id
          LEFT JOIN intervention_uptake iu
            ON iu.intervention_id = il.intervention_id
           AND iu.group_id = il.group_id
           AND (iu.session_id = il.session_id OR iu.session_id IS NULL)
         WHERE il.group_id=?
           {session_filter}
           AND il.created_at>=?
           AND il.created_at<=?
         ORDER BY il.created_at ASC, il.id ASC
        """.format(session_filter=session_filter),
        tuple(params),
    )

    by_log = {m.get("linked_log_id"): m for m in messages if m.get("linked_log_id")}
    by_run = {m.get("intervention_run_id"): m for m in messages if m.get("intervention_run_id")}
    result = []
    for row in rows:
        d = dict(row)
        d["trigger_source"] = d.get("trigger_source") or d.get("run_trigger_type")
        d["agent_type"] = d.get("agent_type") or d.get("run_agent_type")
        d["help_request_id"] = d.get("help_request_id") or d.get("run_help_request_id")
        linked = by_log.get(d.get("id")) or by_run.get(d.get("intervention_id"))
        state = _map_state_code(d.get("detected_state"))
        kind = _intervention_kind(d)
        trigger_type = d.get("trigger_source") or d.get("push_mode")
        result.append({
            "id": d.get("id"),
            "intervention_id": d.get("intervention_id"),
            "created_at": d.get("created_at"),
            "actor": _actor_for_kind(kind),
            "trigger_type": trigger_type,
            "push_mode": d.get("push_mode"),
            "trigger_source": d.get("trigger_source"),
            "title": d.get("title"),
            "message": d.get("message") or "",
            "condition": d.get("condition"),
            "strategy_id": d.get("strategy_id"),
            "strategy_type": d.get("strategy_type"),
            "sub_category": d.get("sub_category"),
            "model_name": d.get("model_name"),
            "detected_state": state if state != OBSERVING_STATE else None,
            "detected_state_label": _map_state_label(state) if state and state != OBSERVING_STATE else None,
            "run_confidence": d.get("run_confidence"),
            "run_status": d.get("run_status"),
            "cutoff_sequence": d.get("cutoff_sequence"),
            "target_segment_id": d.get("target_segment_id"),
            "state_assessment_id": d.get("target_state_assessment_id"),
            "generated_at": d.get("generated_at"),
            "published_at": d.get("published_at"),
            "failure_reason": d.get("failure_reason"),
            "auto_uptake_type": d.get("auto_uptake_type"),
            "manual_uptake_type": d.get("manual_uptake_type"),
            "intervention_kind": kind,
            "display_label": _agent_label(kind),
            "linked_message_id": linked.get("id") if linked else None,
            "linked_sequence": linked.get("sequence") if linked else d.get("cutoff_sequence"),
        })
    return result


def _summary(
    messages,
    state_segments,
    silence_segments,
    interventions,
    trend_summary,
    assignment_summary=None,
):
    assignment_summary = assignment_summary or {}
    student_messages = [m for m in messages if m.get("role") == "student"]
    agent_messages = [m for m in messages if m.get("role") == "agent"]
    observing_count = sum(
        1 for m in student_messages if m.get("assessment_status") == "observing"
    )
    unclassified_count = sum(
        1 for m in student_messages if m.get("assessment_status") == "unclassified"
    )
    confirmed_count = sum(
        1 for m in student_messages if m.get("assessment_status") == "confirmed"
    )
    return {
        "message_count": len(messages),
        "student_message_count": len(student_messages),
        "agent_message_count": len(agent_messages),
        "active_student_count": len({m.get("user_id") for m in student_messages if m.get("user_id")}),
        "snapshot_count": len(state_segments or []) + len(silence_segments or []),
        "state_segment_count": len(state_segments or []),
        "silence_segment_count": len(silence_segments or []),
        "intervention_count": len(interventions or []),
        "observing_student_message_count": observing_count,
        "unclassified_student_message_count": unclassified_count,
        "confirmed_student_message_count": confirmed_count,
        "display_assigned_student_message_count": assignment_summary.get(
            "display_assigned_student_message_count",
            confirmed_count + observing_count + unclassified_count,
        ),
        "student_display_assignment_rate": assignment_summary.get(
            "student_display_assignment_rate",
        ),
        "precise_sub_state_message_count": assignment_summary.get(
            "precise_sub_state_message_count",
            confirmed_count,
        ),
        "precise_sub_state_coverage_rate": assignment_summary.get(
            "precise_sub_state_coverage_rate",
        ),
        "inferred_assignment_count": assignment_summary.get(
            "inferred_assignment_count",
            sum(1 for m in student_messages if m.get("inferred")),
        ),
        "legacy_monitor_only_message_count": assignment_summary.get(
            "legacy_monitor_only_message_count",
            sum(
                1
                for m in student_messages
                if m.get("assignment_source") == "legacy_monitor_only"
            ),
        ),
        "message_assignment_policy": MESSAGE_STATE_ASSIGNMENT_POLICY,
        "message_assignment_summary": assignment_summary,
        "student_state_count_invariant": (
            confirmed_count + observing_count + unclassified_count
            == len(student_messages)
        ),
        "latest_state_code": trend_summary.get("latest_state_code") or OBSERVING_STATE,
        "latest_state_label": trend_summary.get("latest_state_label") or STATE_LABEL_MAP[OBSERVING_STATE],
        "latest_assessment_status": (
            trend_summary.get("latest_assessment_status") or "observing"
        ),
        "latest_state_confidence": trend_summary.get("latest_state_confidence"),
        "latest_state_at": trend_summary.get("latest_state_at"),
        "latest_state_source": trend_summary.get("latest_state_source") or "observing_default",
        "duration_note": trend_summary.get("duration_note"),
    }


def get_emotion_review(
    group_id,
    session_id=None,
    start_time=None,
    end_time=None,
    window_minutes=5,
    include_legacy_scope=False,
):
    """Return a unified packet for the T4 review-style visualization."""
    group = query_one("SELECT id, name, group_code FROM groups WHERE id=?", (group_id,))
    if not group:
        return {"error": "Group %d not found" % group_id}

    trend = get_emotion_trend(
        group_id=group_id,
        session_id=session_id,
        start_time=start_time,
        end_time=end_time,
        window_minutes=0,
        include_legacy_scope=include_legacy_scope,
    )
    if "error" in trend:
        return trend

    ws = trend.get("time_range", {}).get("start")
    we = trend.get("time_range", {}).get("end")
    if not ws or not we:
        ws, we, _legacy_warning = _resolve_time_range(
            session_id,
            start_time,
            end_time,
            group_id=group_id,
        )
    resolved_session_id = trend.get("resolved_session_id")
    scope = trend.get("session") or _session_scope(resolved_session_id)
    bucket_minutes = _coerce_bucket_minutes(window_minutes)

    state_segments = list(trend.get("state_segments") or [])
    silence_segments = list(trend.get("silence_segments") or [])
    snapshots = list(trend.get("snapshots") or trend.get("state_snapshots") or [])

    messages = _fetch_messages(
        group_id,
        resolved_session_id,
        scope,
        ws,
        we,
        include_legacy_scope=include_legacy_scope,
    )
    assignment_payload = _assign_state_to_messages(
        messages,
        state_segments,
        trend.get("message_state_context") or {},
        silence_segments,
        group_id=group_id,
        session_id=resolved_session_id,
        discussion_id=(trend.get("message_state_context") or {}).get(
            "discussion_id"
        ),
    )
    for snap in snapshots:
        snap.update(_snapshot_sequences(snap, messages))

    interventions = _fetch_interventions(
        group_id,
        resolved_session_id,
        scope,
        ws,
        we,
        messages,
        include_legacy_scope=include_legacy_scope,
    )
    _annotate_agent_messages(messages, interventions)

    return {
        "group_id": group_id,
        "group_name": group["name"],
        "group_code": group["group_code"],
        "session_id": session_id,
        "requested_session_id": session_id,
        "resolved_session_id": resolved_session_id,
        "scope_mode": trend.get("scope_mode"),
        "scope_resolved_from": trend.get("scope_resolved_from"),
        "fallback_reason": (
            trend.get("fallback_reason")
            or (
                "explicit_legacy_scope_enabled"
                if include_legacy_scope
                else None
            )
        ),
        "legacy_scope_fallback": bool(include_legacy_scope),
        "session": scope,
        "window_minutes": bucket_minutes,
        "generated_at": now_str(),
        "time_range": {
            "start": ws,
            "end": we,
            "legacy_data_warning": bool(trend.get("legacy_data_warning")),
        },
        "state_system": trend.get("state_system") or [],
        "detailed_state_system": trend.get("detailed_state_system") or [],
        "coarse_state_system": trend.get("coarse_state_system") or [],
        "coarse_state_debug_only": True,
        "state_source_mode": trend.get("state_source_mode") or "canonical",
        "state_source_policy": (
            trend.get("state_source_policy")
            or CANONICAL_STATE_SOURCE_POLICY
        ),
        "message_state_context": trend.get("message_state_context") or {},
        "message_assignment_policy": MESSAGE_STATE_ASSIGNMENT_POLICY,
        "message_assignment_summary": assignment_payload.get("summary") or {},
        "current_state": trend.get("current_state") or {
            "semantic_state": None,
            "assessment_status": UNCLASSIFIED_STATE,
        },
        "active_silence": trend.get("active_silence") or {"active": False},
        "summary": _summary(
            messages,
            state_segments,
            silence_segments,
            interventions,
            trend.get("summary") or {},
            assignment_payload.get("summary") or {},
        ),
        "messages": messages,
        "state_segments": state_segments,
        "silence_segments": silence_segments,
        "state_snapshots": snapshots,
        "state_runs": _state_runs(snapshots),
        "distribution": trend.get("distribution") or {
            code: {
                "state_code": code,
                "state_label": STATE_LABEL_MAP[code],
                "segment_count": 0,
                "message_count": 0,
                "duration_seconds": 0,
            }
            for code in FORMAL_STATE_ORDER
        },
        "detailed_distribution": trend.get("detailed_distribution") or {},
        "coarse_distribution": trend.get("coarse_distribution") or {},
        "transitions": trend.get("transitions") or [],
        "participation": _participation(messages),
        "participation_timeline": _participation_timeline(messages, bucket_minutes),
        "interventions": interventions,
        "quality_warnings": trend.get("quality_warnings") or [],
        "excluded_segment_count": trend.get("excluded_segment_count", 0),
        "excluded_segment_reasons": trend.get("excluded_segment_reasons") or {},
    }
