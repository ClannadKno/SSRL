# -*- coding: utf-8 -*-
"""T7: Teacher export service.

Provides CSV generation for 15 export types with direct-view defaults and
optional blind mode. Blind mode hides: real_name, condition, talk_library,
agent full content.

Package-level metadata such as schema_version and generated_at belongs in the
ZIP manifest. Row-level CSVs only include data fields or real audit/source
fields from the underlying tables.
"""

import csv
import io
import json
import re

from db import query_all
from knowledge_base import normalize_state_payload
from services.message_state_assignment_service import assign_message_states
from services.three_stage_schema import (
    FINAL_SUB_STATE_LABELS,
    is_primary_sub_state,
)


SCHEMA_VERSION = "2.0"
STATE_EXPORT_SYSTEM_VERSION = "CANONICAL_FINAL_STATE_EXPORT_V2"
HISTORY_NOT_RECORDED = "历史数据未记录"


def _student_group_no_expr(role_alias="u"):
    return (
        "CASE WHEN {role_alias}.role='student' THEN ep.group_no ELSE NULL END"
    ).format(role_alias=role_alias)


def _student_member_no_expr(role_alias="u"):
    return (
        "CASE WHEN {role_alias}.role='student' THEN ep.member_no ELSE NULL END"
    ).format(role_alias=role_alias)


def _group_no_from_code(group_code):
    if not group_code:
        return ""
    m = re.match(r"^[Gg]\s*0*(\d+)$", str(group_code).strip())
    return str(int(m.group(1))) if m else ""


def _json_dumps_cell(value):
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads_cell(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list_cell(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, list):
        return parsed
    return [value]


def _table_columns(table_name):
    return [d["name"] for d in query_all("PRAGMA table_info('%s')" % table_name)]


def _table_exists(table_name):
    return bool(_table_columns(table_name))


def _session_sequence_set(group_id, session_id):
    if session_id is None:
        return None
    if group_id is not None:
        rows = query_all(
            """
            SELECT sequence
              FROM messages
             WHERE group_id=? AND session_id=? AND sequence IS NOT NULL
            """,
            (int(group_id), int(session_id)),
        )
    else:
        rows = query_all(
            """
            SELECT sequence
              FROM messages
             WHERE session_id=? AND sequence IS NOT NULL
            """,
            (int(session_id),),
        )
    return {int(r["sequence"]) for r in rows if r["sequence"] is not None}


def _review_row_matches_session(row, session_sequences):
    if session_sequences is None:
        return True
    try:
        if row.get("cutoff_sequence") is not None and int(row.get("cutoff_sequence")) in session_sequences:
            return True
    except (TypeError, ValueError):
        pass
    for key in ("input_message_sequences_json", "evidence_sequences_json"):
        for seq in _json_list_cell(row.get(key)):
            try:
                if int(seq) in session_sequences:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _review_rule_candidate_state(row):
    payload = _json_loads_cell(row.get("rule_result_json") or row.get("monitor_rule_result_json"))
    for key in ("state_code", "rule_state_code", "final_state", "primary_state"):
        if payload.get(key):
            return payload.get(key)
    candidates = payload.get("candidates")
    if isinstance(candidates, list) and candidates:
        first = candidates[0]
        if isinstance(first, dict):
            return first.get("state_code") or first.get("code") or first.get("state")
    return row.get("final_state") or row.get("monitor_final_state") or row.get("run_detected_state")


def _merge_tags(tags, value):
    if not value:
        return
    if isinstance(value, dict):
        values = list(value.keys()) + list(value.values())
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        text = str(value)
        if "evidence_tags=" in text:
            text = text.split("evidence_tags=", 1)[1]
            text = text.split(";", 1)[0]
        values = text.replace("，", ",").replace("；", ",").replace(";", ",").split(",")
    for item in values:
        tag = str(item or "").strip()
        if tag and tag not in tags:
            tags.append(tag)


def _collect_evidence_tags(*values):
    tags = []
    for value in values:
        if not value:
            continue
        if isinstance(value, dict):
            for key in ("evidence_tags", "secondary_flags", "tags"):
                _merge_tags(tags, value.get(key))
            _merge_tags(tags, value.get("coordination_evidence_tags"))
            _merge_tags(tags, value.get("legacy_state_code"))
            signals = value.get("signals") if isinstance(value.get("signals"), dict) else {}
            _merge_tags(tags, signals.get("evidence_tags"))
            _merge_tags(tags, signals.get("coordination_evidence_tags"))
            result = value.get("result") if isinstance(value.get("result"), dict) else {}
            _merge_tags(tags, result.get("evidence_tags"))
            validation = value.get("validation") if isinstance(value.get("validation"), dict) else {}
            _merge_tags(tags, validation.get("evidence_tags"))
            check = validation.get("triggerable_state_check") if isinstance(validation.get("triggerable_state_check"), dict) else {}
            _merge_tags(tags, check.get("evidence_tags"))
        else:
            _merge_tags(tags, value)
    return tags


def _merge_score(scores, key, value):
    if key in (None, "") or value in (None, ""):
        return
    try:
        scores[str(key)] = float(value)
    except (TypeError, ValueError):
        return


def _collect_candidate_scores(*payloads, state_score=None, confidence=None):
    scores = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in ("candidate_scores", "state_scores", "scores", "score_by_state"):
            value = payload.get(key)
            if isinstance(value, dict):
                for state_code, score in value.items():
                    _merge_score(scores, state_code, score)
        candidates = payload.get("candidates")
        if isinstance(candidates, list):
            for item in candidates:
                if isinstance(item, dict):
                    _merge_score(
                        scores,
                        item.get("state_code") or item.get("code") or item.get("state"),
                        item.get("score") or item.get("confidence"),
                    )
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        if result:
            _merge_score(
                scores,
                result.get("state_code") or result.get("primary_state"),
                result.get("score") or result.get("confidence"),
            )
    _merge_score(scores, "raw_state_score", state_score)
    _merge_score(scores, "final_confidence", confidence)
    return scores


def _normalize_export_state(raw_state_code, *, row=None, evidence_tags=None,
                            payloads=(), state_score=None, confidence=None):
    row = row or {}
    status = row.get("assessment_status")
    code = raw_state_code
    if str(status or "").strip().lower() == "insufficient_evidence":
        code = "insufficient_evidence"
    tags = _collect_evidence_tags(*(payloads or ()), evidence_tags, row.get("evidence"), row.get("evidence_summary"))
    normalized = normalize_state_payload(
        code,
        evidence_tags=tags,
        assessment_status=status,
    )
    candidate_scores = _collect_candidate_scores(
        *(payloads or ()),
        state_score=state_score,
        confidence=confidence,
    )
    normalization_reason = normalized.get("normalization_reason")
    legacy_state_code = normalized.get("legacy_state_code")
    for payload in payloads or ():
        if not isinstance(payload, dict):
            continue
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        normalization_reason = (
            payload.get("normalization_reason")
            or result.get("normalization_reason")
        ) or normalization_reason
        legacy_state_code = (
            payload.get("legacy_state_code")
            or payload.get("rule_legacy_state_code")
            or payload.get("llm_legacy_state_code")
            or result.get("legacy_state_code")
        ) or legacy_state_code

    return {
        "state_code": normalized["state_code"],
        "state_label": normalized["state_label"],
        "legacy_state_code": legacy_state_code or "",
        "normalization_reason": normalization_reason or "",
        "evidence_tags": normalized["evidence_tags"],
        "candidate_scores": candidate_scores,
    }


def _assessment_payloads(row):
    return [
        _json_loads_cell(row.get("fusion_json")),
        _json_loads_cell(row.get("rule_result_json") or row.get("rule_assessment_json")),
        _json_loads_cell(row.get("llm_result_json") or row.get("llm_assessment_json")),
        _json_loads_cell(row.get("context_json")),
        _json_loads_cell(row.get("feature_json")),
    ]


def _assessment_export_record(row):
    d = dict(row)
    payloads = _assessment_payloads(d)
    raw_state = (
        d.get("fused_state_code")
        or payloads[0].get("fused_state_code")
        or d.get("state_code")
        or d.get("rule_state_code")
        or d.get("llm_state_code")
    )
    state = _normalize_export_state(
        raw_state,
        row=d,
        evidence_tags=_collect_evidence_tags(*payloads),
        payloads=payloads,
        state_score=d.get("state_score"),
        confidence=d.get("confidence"),
    )
    d["raw_state_code"] = d.get("state_code") or raw_state or ""
    d["coarse_state_code"] = state["state_code"]
    d["coarse_state_label"] = state["state_label"]
    d["llm_coarse_state_code"] = d.get("llm_state_code") or ""
    d["fused_coarse_state_code"] = d.get("fused_state_code") or ""
    d["legacy_state_code"] = state["legacy_state_code"]
    d["normalization_reason"] = state["normalization_reason"]
    d["evidence_tags"] = _json_dumps_cell(state["evidence_tags"])
    d["candidate_scores_json"] = _json_dumps_cell(state["candidate_scores"])
    # Compatibility-only aliases are explicit legacy fields and are appended
    # after the primary coarse detector contract.
    d["final_state_code_legacy"] = state["state_code"]
    d["final_state_label_legacy"] = state["state_label"]
    d["export_schema_version"] = SCHEMA_VERSION
    d["state_system_version"] = STATE_EXPORT_SYSTEM_VERSION
    return d


def _state_export_extra_fields():
    return [
        "export_schema_version",
        "coarse_state_code", "coarse_state_label",
        "llm_coarse_state_code", "fused_coarse_state_code",
        "raw_state_code", "legacy_state_code", "normalization_reason",
        "evidence_tags", "candidate_scores_json", "state_system_version",
        "final_state_code_legacy", "final_state_label_legacy",
    ]


def _load_assessment_refs(group_id=None, session_id=None, task_id=None,
                          start_time=None, end_time=None):
    columns = _table_columns("state_assessments")
    if not columns:
        return []

    sql = """SELECT sa.*
             FROM state_assessments sa
             WHERE 1=1"""
    params = []
    if group_id is not None:
        sql += " AND sa.group_id = ?"
        params.append(int(group_id))
    if session_id is not None and "session_id" in columns:
        sql += " AND sa.session_id = ?"
        params.append(int(session_id))
    if task_id is not None and "task_id" in columns:
        sql += " AND sa.task_id = ?"
        params.append(int(task_id))
    if start_time:
        sql += " AND (sa.created_at >= ? OR sa.window_end >= ?)"
        params.extend([start_time, start_time])
    if end_time:
        sql += " AND (sa.created_at <= ? OR sa.window_start <= ?)"
        params.extend([end_time, end_time])
    sql += " ORDER BY sa.group_id ASC, COALESCE(sa.session_id, 0) ASC, COALESCE(sa.task_id, 0) ASC, sa.created_at ASC, sa.id ASC"
    return [_assessment_export_record(dict(row)) for row in query_all(sql, tuple(params))]


def _same_scope(message, assessment):
    if str(message.get("group_id") or "") != str(assessment.get("group_id") or ""):
        return False
    for key in ("session_id", "task_id"):
        message_value = message.get(key)
        assessment_value = assessment.get(key)
        if message_value not in (None, "") and assessment_value not in (None, ""):
            if str(message_value) != str(assessment_value):
                return False
    return True


def _find_assigned_assessment(message, assessments):
    created_at = str(message.get("created_at") or "")
    scoped = [item for item in assessments if _same_scope(message, item)]
    if not scoped:
        return None

    window_matches = []
    for item in scoped:
        window_start = str(item.get("window_start") or "")
        window_end = str(item.get("window_end") or "")
        if window_start and window_end and window_start <= created_at <= window_end:
            window_matches.append(item)
    if window_matches:
        return window_matches[-1]

    before = [
        item for item in scoped
        if str(item.get("created_at") or "") <= created_at
    ]
    if before:
        return before[-1]
    return scoped[0]


def _apply_assigned_state(message, assessments):
    assessment = _find_assigned_assessment(message, assessments)
    if not assessment:
        message.update({
            "assigned_state_assessment_id": "",
            "assigned_state_code": "",
            "assigned_state_label": "",
            "assigned_legacy_state_code": "",
            "assigned_normalization_reason": "",
            "assigned_evidence_tags": "",
            "assigned_state_window_start": "",
            "assigned_state_window_end": "",
        })
        return
    message.update({
        "assigned_state_assessment_id": assessment.get("id") or "",
        "assigned_state_code": assessment.get("state_code") or "",
        "assigned_state_label": assessment.get("state_label") or "",
        "assigned_legacy_state_code": assessment.get("legacy_state_code") or "",
        "assigned_normalization_reason": assessment.get("normalization_reason") or "",
        "assigned_evidence_tags": assessment.get("evidence_tags") or "",
        "assigned_state_window_start": assessment.get("window_start") or "",
        "assigned_state_window_end": assessment.get("window_end") or "",
    })


def _apply_intervention_trigger_state(row):
    metadata = _json_loads_cell(row.get("run_metadata_json"))
    trigger_reason = _json_loads_cell(row.get("run_trigger_reason_json"))
    candidate_list = _json_list_cell(row.get("run_candidate_strategies"))
    selected_strategy = _json_loads_cell(row.get("run_selected_strategy"))
    payloads = [metadata, trigger_reason, selected_strategy]
    raw_state = (
        row.get("run_detected_state")
        or metadata.get("final_state_code")
        or metadata.get("detected_state")
        or trigger_reason.get("state_code")
    )
    state = _normalize_export_state(
        raw_state,
        evidence_tags=_collect_evidence_tags(*payloads),
        payloads=payloads,
        confidence=row.get("run_confidence"),
    )
    if candidate_list and not state["candidate_scores"]:
        state["candidate_scores"] = {
            "candidate_%d" % (index + 1): item
            for index, item in enumerate(candidate_list)
            if isinstance(item, (int, float))
        }
    row["trigger_state_code"] = state["state_code"]
    row["trigger_state_label"] = state["state_label"]
    row["trigger_legacy_state_code"] = state["legacy_state_code"]
    row["trigger_normalization_reason"] = state["normalization_reason"]
    row["trigger_evidence_tags"] = _json_dumps_cell(state["evidence_tags"])
    row["trigger_candidate_scores_json"] = _json_dumps_cell(state["candidate_scores"])


def _int_or_none(value):
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_export_assignment_context(group_id, session_id, discussion_id):
    context = {
        "discussion_id": discussion_id,
        "has_assessment_pipeline": False,
        "batch_windows": [],
        "last_finalized_student_sequence": None,
        "last_scheduled_student_sequence": None,
        "observation_status": "inactive",
        "observation_started_sequence": None,
        "last_intervention_sequence": None,
    }
    if session_id is None or discussion_id is None:
        return context

    batches = [
        dict(row)
        for row in query_all(
            """
            SELECT id, status, terminal_status, error_code, fallback_action,
                   fallback_segment_count, candidate_start_sequence,
                   candidate_end_sequence
              FROM state_assessment_batches
             WHERE group_id=? AND session_id=? AND discussion_id=?
             ORDER BY id ASC
            """,
            (int(group_id), int(session_id), int(discussion_id)),
        )
    ]
    cursor_rows = query_all(
        """
        SELECT *
          FROM discussion_assessment_cursors
         WHERE group_id=? AND session_id=? AND discussion_id=?
         LIMIT 1
        """,
        (int(group_id), int(session_id), int(discussion_id)),
    )
    cursor = dict(cursor_rows[0]) if cursor_rows else {}
    context["has_assessment_pipeline"] = bool(batches or cursor)
    context["batch_windows"] = [
        {
            "batch_id": batch.get("id"),
            "status": batch.get("status"),
            "terminal_status": batch.get("terminal_status"),
            "error_code": batch.get("error_code"),
            "fallback_action": batch.get("fallback_action"),
            "fallback_segment_count": batch.get("fallback_segment_count"),
            "start_sequence": batch.get("candidate_start_sequence"),
            "end_sequence": batch.get("candidate_end_sequence"),
        }
        for batch in batches
    ]
    if cursor:
        context.update({
            "last_finalized_student_sequence": cursor.get(
                "last_finalized_student_sequence"
            ),
            "last_scheduled_student_sequence": cursor.get(
                "last_scheduled_student_sequence"
            ),
            "observation_status": cursor.get("observation_status") or "inactive",
            "observation_started_sequence": cursor.get(
                "observation_started_sequence"
            ),
            "last_intervention_sequence": cursor.get(
                "last_intervention_sequence"
            ),
        })
    return context


def _load_strategy_pipeline_refs(group_id, session_id):
    if not _table_exists("strategy_pipeline_runs"):
        return []
    sql = """
        SELECT id, group_id, session_id, discussion_id, task_id,
               input_start_sequence, input_end_sequence,
               sub_state_start_sequence, sub_state_end_sequence,
               canonical_sub_state_code, should_intervene,
               selected_strategy_id, inhibition_strategy_id,
               publish_status, final_status, failure_code, failure_detail,
               published_message_id, created_at, updated_at
          FROM strategy_pipeline_runs
         WHERE group_id=?
    """
    params = [int(group_id)]
    if session_id is None:
        sql += " AND session_id IS NULL"
    else:
        sql += " AND session_id=?"
        params.append(int(session_id))
    sql += " ORDER BY id ASC"
    return [dict(row) for row in query_all(sql, tuple(params))]


def _same_optional_scope(left, right):
    if left in (None, "") or right in (None, ""):
        return left in (None, "") and right in (None, "")
    return str(left) == str(right)


def _pipeline_for_assignment(message, assignment, pipelines):
    segment = assignment.get("segment") or {}
    direct_id = _int_or_none(segment.get("strategy_pipeline_run_id"))
    if direct_id is not None:
        direct = next(
            (item for item in pipelines if _int_or_none(item.get("id")) == direct_id),
            None,
        )
        if direct:
            return direct

    published = [
        item
        for item in pipelines
        if _int_or_none(item.get("published_message_id"))
        == _int_or_none(message.get("id"))
    ]
    if published:
        return max(published, key=lambda item: _int_or_none(item.get("id")) or 0)

    sequence = _int_or_none(message.get("sequence"))
    if sequence is None:
        return None
    final_code = assignment.get("final_sub_state_code")
    candidates = []
    for item in pipelines:
        if not _same_optional_scope(
            message.get("discussion_id"), item.get("discussion_id")
        ):
            continue
        if not _same_optional_scope(message.get("task_id"), item.get("task_id")):
            continue
        start = _int_or_none(
            item.get("sub_state_start_sequence")
            if item.get("sub_state_start_sequence") is not None
            else item.get("input_start_sequence")
        )
        end = _int_or_none(
            item.get("sub_state_end_sequence")
            if item.get("sub_state_end_sequence") is not None
            else item.get("input_end_sequence")
        )
        if start is None or end is None or not start <= sequence <= end:
            continue
        pipeline_code = str(item.get("canonical_sub_state_code") or "").strip()
        if final_code and pipeline_code and pipeline_code != final_code:
            continue
        candidates.append(item)
    return (
        max(candidates, key=lambda item: _int_or_none(item.get("id")) or 0)
        if candidates
        else None
    )


def _canonical_scope_segments(group_id, session_id, start_time, end_time):
    if session_id is None:
        return [], []
    from services.teacher_emotion_trend_service import get_emotion_trend

    trend = get_emotion_trend(
        group_id=int(group_id),
        session_id=int(session_id),
        start_time=start_time,
        end_time=end_time,
        window_minutes=0,
        include_legacy_scope=False,
    )
    if trend.get("error"):
        return [], []
    return (
        list(trend.get("state_segments") or []),
        list(trend.get("silence_segments") or []),
    )


def _scoped_silence_segments(silence_segments, discussion_id):
    if discussion_id is None:
        return [
            item
            for item in silence_segments
            if item.get("discussion_id") in (None, "")
        ]
    return [
        item
        for item in silence_segments
        if _int_or_none(item.get("discussion_id")) == int(discussion_id)
    ]


def _scoped_state_segments(state_segments, discussion_id):
    if discussion_id is None:
        return [
            item
            for item in state_segments
            if item.get("discussion_id") in (None, "")
        ]
    return [
        item
        for item in state_segments
        if _int_or_none(item.get("discussion_id")) == int(discussion_id)
    ]


def _decorate_message_assignment(message, assignment, pipeline):
    segment = assignment.get("segment") or {}
    final_code = (
        assignment.get("final_sub_state_code")
        if is_primary_sub_state(assignment.get("final_sub_state_code"))
        else None
    )
    selected_strategy_id = (
        (pipeline or {}).get("selected_strategy_id")
        or segment.get("selected_strategy_id")
        or (
            message.get("strategy_id")
            if assignment.get("role") != "student"
            else None
        )
    )
    inhibition_strategy_id = (
        (pipeline or {}).get("inhibition_strategy_id")
        or segment.get("inhibition_strategy_id")
    )
    if str(selected_strategy_id or "").startswith("OI-"):
        inhibition_strategy_id = inhibition_strategy_id or selected_strategy_id
        selected_strategy_id = None

    row = dict(message)
    row.update({
        "export_schema_version": SCHEMA_VERSION,
        "message_id": message.get("id"),
        "display_state_code": assignment.get("display_state_code") or "",
        "display_state_label": assignment.get("display_state_label") or "",
        "final_sub_state_code": final_code or "",
        "final_sub_state_label": (
            FINAL_SUB_STATE_LABELS.get(final_code, "") if final_code else ""
        ),
        "context_sub_state_code": (
            assignment.get("context_state_code") or ""
        ),
        "assessment_status": assignment.get("assessment_status") or "",
        "assignment_source": assignment.get("assignment_source") or "",
        "state_assignment_reason": (
            assignment.get("state_assignment_reason") or ""
        ),
        "inferred": 1 if assignment.get("inferred") else 0,
        "coarse_state_code": assignment.get("coarse_state_code") or "",
        "legacy_state_code": assignment.get("legacy_state_code") or "",
        "state_overlays": _json_dumps_cell(
            assignment.get("state_overlays") or []
        ),
        "confidence": (
            assignment.get("confidence")
            if assignment.get("confidence") is not None
            else ""
        ),
        "segment_id": assignment.get("source_segment_id") or "",
        "assessment_batch_id": assignment.get("source_batch_id") or "",
        "strategy_pipeline_run_id": (pipeline or {}).get("id") or (
            segment.get("strategy_pipeline_run_id") or ""
        ),
        "selected_strategy_id": selected_strategy_id or "",
        "inhibition_strategy_id": inhibition_strategy_id or "",
        "should_intervene": (
            (pipeline or {}).get("should_intervene")
            if (pipeline or {}).get("should_intervene") is not None
            else segment.get("should_intervene")
            if segment.get("should_intervene") is not None
            else ""
        ),
        "error_code": (
            assignment.get("error_code")
            or (pipeline or {}).get("failure_code")
            or ""
        ),
        "failure_detail": (pipeline or {}).get("failure_detail") or "",
        # Backward-compatible message assignment aliases. These are appended
        # after the canonical fields and now project the canonical read model.
        "assigned_state_assessment_id": segment.get("assessment_id") or "",
        "assigned_state_code": final_code or "",
        "assigned_state_label": (
            FINAL_SUB_STATE_LABELS.get(final_code, "") if final_code else ""
        ),
        "assigned_legacy_state_code": (
            assignment.get("legacy_state_code") or ""
        ),
        "assigned_normalization_reason": (
            assignment.get("state_assignment_reason") or ""
        ),
        "assigned_evidence_tags": _json_dumps_cell(
            assignment.get("state_overlays") or []
        ),
        "assigned_state_window_start": (
            segment.get("start_sequence")
            if segment.get("start_sequence") is not None
            else ""
        ),
        "assigned_state_window_end": (
            segment.get("end_sequence")
            if segment.get("end_sequence") is not None
            else ""
        ),
    })
    return row


def _load_message_export_records(
    group_id=None,
    session_id=None,
    task_id=None,
    start_time=None,
    end_time=None,
):
    sql = """SELECT m.id, m.group_id, m.user_id, u.participant_code,
                    COALESCE(ep.display_name, '') AS display_name,
                    g.group_code, g.condition,
                    """ + _student_group_no_expr("u") + """ AS group_no,
                    """ + _student_member_no_expr("u") + """ AS member_no,
                    m.sequence, m.sender_type, m.session_no, m.task_id,
                    m.session_id, m.discussion_id,
                    COALESCE(NULLIF(TRIM(m.role), ''), u.role) AS role,
                    m.strategy_id, m.reply_to_message_id, m.client_message_id,
                    m.linked_log_id, m.intervention_run_id, m.agent_type,
                    COALESCE(m.trigger_source, il.trigger_source, ir.trigger_type) AS trigger_source,
                    m.content, m.created_at
             FROM messages m
             JOIN groups g ON m.group_id=g.id
             JOIN users u ON m.user_id=u.id
             LEFT JOIN experiment_participants ep ON m.user_id=ep.user_id AND m.group_id=ep.group_id
             LEFT JOIN intervention_logs il ON m.linked_log_id=il.id
             LEFT JOIN intervention_runs ir
               ON ir.id=m.intervention_run_id
               OR (il.intervention_id IS NOT NULL AND ir.id=il.intervention_id)
             WHERE 1=1"""
    params = []
    if group_id is not None:
        sql += " AND m.group_id = ?"
        params.append(int(group_id))
    if session_id is not None:
        sql += " AND m.session_id = ?"
        params.append(int(session_id))
    if task_id is not None:
        sql += " AND m.task_id = ?"
        params.append(int(task_id))
    if start_time:
        sql += " AND m.created_at >= ?"
        params.append(start_time)
    if end_time:
        sql += " AND m.created_at <= ?"
        params.append(end_time)
    sql += " ORDER BY m.group_id ASC, m.session_id ASC, m.discussion_id ASC, m.sequence ASC, m.id ASC"

    messages = [dict(row) for row in query_all(sql, tuple(params))]
    scoped_messages = {}
    for message in messages:
        key = (
            _int_or_none(message.get("group_id")),
            _int_or_none(message.get("session_id")),
        )
        scoped_messages.setdefault(key, []).append(message)

    assignments_by_message_id = {}
    pipelines_by_scope = {}
    for (scope_group_id, scope_session_id), scope_rows in scoped_messages.items():
        scope_times = [
            str(item.get("created_at"))
            for item in scope_rows
            if item.get("created_at")
        ]
        scope_start_time = start_time or (
            min(scope_times) if scope_times else None
        )
        scope_end_time = end_time or (
            max(scope_times) if scope_times else None
        )
        state_segments, silence_segments = _canonical_scope_segments(
            scope_group_id,
            scope_session_id,
            scope_start_time,
            scope_end_time,
        )
        pipelines = _load_strategy_pipeline_refs(
            scope_group_id,
            scope_session_id,
        )
        pipelines_by_scope[(scope_group_id, scope_session_id)] = pipelines
        by_discussion = {}
        for message in scope_rows:
            by_discussion.setdefault(
                _int_or_none(message.get("discussion_id")),
                [],
            ).append(message)
        for scope_discussion_id, discussion_rows in by_discussion.items():
            context = _load_export_assignment_context(
                scope_group_id,
                scope_session_id,
                scope_discussion_id,
            )
            assignment_payload = assign_message_states(
                messages=discussion_rows,
                state_segments=_scoped_state_segments(
                    state_segments,
                    scope_discussion_id,
                ),
                display_context=context,
                silence_segments=_scoped_silence_segments(
                    silence_segments,
                    scope_discussion_id,
                ),
                group_id=scope_group_id,
                session_id=scope_session_id,
                discussion_id=scope_discussion_id,
            )
            assignments_by_message_id.update(
                assignment_payload.get("by_message_id") or {}
            )

    result = []
    for message in messages:
        assignment = assignments_by_message_id.get(message.get("id")) or {}
        scope_key = (
            _int_or_none(message.get("group_id")),
            _int_or_none(message.get("session_id")),
        )
        pipeline = _pipeline_for_assignment(
            message,
            assignment,
            pipelines_by_scope.get(scope_key, []),
        )
        result.append(_decorate_message_assignment(message, assignment, pipeline))
    return result


def export_messages_csv(group_id=None, session_id=None, task_id=None,
                         start_time=None, end_time=None, blind=False):
    """Export messages.csv from the canonical per-message read model."""
    rows = _load_message_export_records(
        group_id=group_id,
        session_id=session_id,
        task_id=task_id,
        start_time=start_time,
        end_time=end_time,
    )
    return _rows_to_csv(rows, _messages_fields(), _messages_blind, blind,
                         session_id, task_id)


def _messages_fields():
    return [
        "export_schema_version", "message_id", "id",
        "sequence", "sender_type", "role",
        "group_id", "user_id",
        "participant_code", "display_name", "group_code", "condition",
        "group_no", "member_no",
        "session_no", "task_id", "session_id", "discussion_id",
        "strategy_id", "reply_to_message_id", "client_message_id",
        "linked_log_id", "intervention_run_id", "agent_type", "trigger_source",
        "content", "created_at",
        "display_state_code", "display_state_label",
        "final_sub_state_code", "final_sub_state_label",
        "context_sub_state_code", "assessment_status", "assignment_source",
        "state_assignment_reason", "inferred", "coarse_state_code",
        "legacy_state_code", "state_overlays", "confidence",
        "segment_id", "assessment_batch_id", "strategy_pipeline_run_id",
        "should_intervene", "selected_strategy_id", "inhibition_strategy_id",
        "error_code", "failure_detail",
        "assigned_state_assessment_id", "assigned_state_code",
        "assigned_state_label", "assigned_legacy_state_code",
        "assigned_normalization_reason", "assigned_evidence_tags",
        "assigned_state_window_start", "assigned_state_window_end",
    ]


def _messages_blind(row):
    d = dict(row)
    d["condition"] = ""
    d["content"] = _blur_agent_content(d.get("role"), d.get("content"))
    return d


def export_detector_outputs_csv(group_id=None, session_id=None, task_id=None,
                                 start_time=None, end_time=None, blind=False):
    """Export detector_outputs.csv (state_assessments)."""
    fieldnames = [d["name"] for d in query_all("PRAGMA table_info('state_assessments')")]
    session_no_expr = "" if "session_no" in fieldnames else ", COALESCE(es.session_no, '') AS session_no"
    if session_no_expr:
        fieldnames.append("session_no")

    sql = """SELECT sa.*{session_no_expr}
             FROM state_assessments sa
             LEFT JOIN experiment_sessions es ON sa.session_id = es.id
             WHERE 1=1""".format(session_no_expr=session_no_expr)
    params = []
    if group_id is not None:
        sql += " AND sa.group_id = ?"
        params.append(int(group_id))
    if session_id is not None:
        sql += " AND sa.session_id = ?"
        params.append(int(session_id))
    if task_id is not None:
        sql += " AND sa.task_id = ?"
        params.append(int(task_id))
    if start_time:
        sql += " AND sa.created_at >= ?"
        params.append(start_time)
    if end_time:
        sql += " AND sa.created_at <= ?"
        params.append(end_time)
    sql += " ORDER BY sa.id ASC"

    rows = query_all(sql, tuple(params))

    for extra in _state_export_extra_fields():
        if extra not in fieldnames:
            fieldnames.append(extra)

    result_rows = []
    for r in rows:
        d = _assessment_export_record(dict(r))
        if blind:
            d.pop("condition", None)
            d.pop("llm_assessment_json", None)
        result_rows.append(d)

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(result_rows)
    return output.getvalue()


def export_strategy_reviews_csv(group_id=None, session_id=None, task_id=None,
                                start_time=None, end_time=None, blind=False):
    """Export strategy_reviews.csv (monitor_runs unified strategy review audit)."""
    sql = """
        SELECT mr.*,
               mr.id AS monitor_run_id,
               ir.id AS intervention_run_id,
               ir.status AS intervention_run_status,
               ir.agent_type AS run_agent_type,
               ir.trigger_type AS run_trigger_type,
               ir.actual_published_at,
               ir.skip_reason,
               ir.validation_error,
               ir.failure_reason AS intervention_failure_reason
          FROM monitor_runs mr
          LEFT JOIN intervention_runs ir ON ir.monitor_run_id = mr.id
         WHERE 1=1
    """
    params = []
    if group_id is not None:
        sql += " AND mr.group_id = ?"
        params.append(int(group_id))
    if start_time:
        sql += " AND COALESCE(mr.review_started_at, mr.created_at) >= ?"
        params.append(start_time)
    if end_time:
        sql += " AND COALESCE(mr.review_completed_at, mr.completed_at, mr.created_at) <= ?"
        params.append(end_time)
    sql += """
         AND (
              mr.review_decision IS NOT NULL
           OR mr.review_started_at IS NOT NULL
           OR mr.review_error IS NOT NULL
           OR mr.context_from_sequence IS NOT NULL
           OR mr.evidence_sequences_json IS NOT NULL
         )
         ORDER BY mr.group_id ASC, COALESCE(mr.review_started_at, mr.created_at), mr.id ASC
    """

    session_sequences = _session_sequence_set(group_id, session_id)
    rows = []
    for row in query_all(sql, tuple(params)):
        d = dict(row)
        if not _review_row_matches_session(d, session_sequences):
            continue
        failure = (
            d.get("review_error")
            or d.get("intervention_failure_reason")
            or d.get("validation_error")
            or d.get("failure_reason")
        )
        rows.append({
            "id": d.get("monitor_run_id"),
            "monitor_run_id": d.get("monitor_run_id"),
            "intervention_run_id": d.get("intervention_run_id"),
            "group_id": d.get("group_id"),
            "session_id": session_id or "",
            "cutoff_sequence": d.get("cutoff_sequence"),
            "context_from_sequence": d.get("context_from_sequence"),
            "context_to_sequence": d.get("context_to_sequence") or d.get("cutoff_sequence"),
            "input_message_sequences": d.get("input_message_sequences_json") or "",
            "evidence_sequences": d.get("evidence_sequences_json") or "",
            "rule_candidate_state": _review_rule_candidate_state(d),
            "llm_decision": d.get("review_decision"),
            "llm_final_state": d.get("review_final_state"),
            "llm_reason": d.get("review_reason"),
            "review_confidence": d.get("review_confidence"),
            "strategy_id": d.get("selected_strategy_id"),
            "generated_message": "" if blind else (d.get("generated_message") or ""),
            "prompt_version": d.get("prompt_version"),
            "agent_type": d.get("run_agent_type") or "strategy",
            "trigger_source": d.get("run_trigger_type") or d.get("trigger_type"),
            "status": d.get("status"),
            "intervention_run_status": d.get("intervention_run_status"),
            "detected_at": d.get("created_at"),
            "review_started_at": d.get("review_started_at"),
            "review_completed_at": d.get("review_completed_at"),
            "published_at": d.get("actual_published_at"),
            "failure_reason": failure,
            "skip_reason": d.get("skip_reason"),
        })

    fieldnames = [
        "id", "monitor_run_id", "intervention_run_id", "group_id", "session_id",
        "cutoff_sequence", "context_from_sequence", "context_to_sequence",
        "input_message_sequences", "evidence_sequences", "rule_candidate_state",
        "llm_decision", "llm_final_state", "llm_reason", "review_confidence",
        "strategy_id", "generated_message", "prompt_version", "agent_type",
        "trigger_source", "status", "intervention_run_status", "detected_at",
        "review_started_at", "review_completed_at", "published_at",
        "failure_reason", "skip_reason",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _strategy_pipeline_run_fields():
    return [
        "export_schema_version",
        "pipeline_run_id", "run_uuid", "group_id", "group_code", "group_condition",
        "session_id", "session_no", "discussion_id", "task_id",
        "trigger_source", "trigger_message_id", "trigger_priority",
        "input_start_sequence", "input_end_sequence", "input_cutoff_student_sequence",
        "coarse_state_code", "coarse_decision", "coarse_risk_group",
        "coarse_confidence", "coarse_rule_scores_json",
        "coarse_quantitative_features_json", "coarse_evidence_message_ids_json",
        "coarse_reason_codes_json", "raw_sub_state_code",
        "canonical_sub_state_code", "canonical_sub_state_label",
        "state_overlays", "sub_state_confidence",
        "sub_state_start_sequence", "sub_state_end_sequence",
        "evidence_message_ids", "should_intervene", "inhibition_strategy_id",
        "inhibition_reason", "candidate_strategy_ids", "selected_strategy_id",
        "selected_strategy_name", "selected_strategy_type",
        "supporting_strategy_ids", "strategy_selection_reason",
        "strategy_library_version", "generated_intervention_text",
        "validated_intervention_text", "text_validation_result_json",
        "publish_status", "skip_reason", "failure_code", "failure_detail",
        "published_message_id", "published_at", "observation_status",
        "observation_result", "observation_previous_sub_state_code",
        "observation_current_sub_state_code", "observation_details_json",
        "final_status", "created_at", "updated_at",
        "secondary_tags",
    ]


def export_strategy_pipeline_runs_csv(group_id=None, session_id=None, task_id=None,
                                      start_time=None, end_time=None, blind=False):
    """Export strategy_pipeline_runs.csv with the full three-stage audit chain."""
    fieldnames = _strategy_pipeline_run_fields()
    if not _table_exists("strategy_pipeline_runs"):
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        return output.getvalue()

    sql = """
        SELECT spr.*,
               g.group_code,
               g.condition AS group_condition,
               COALESCE(es.session_no, spr.session_no, '') AS export_session_no
          FROM strategy_pipeline_runs spr
          LEFT JOIN groups g ON g.id = spr.group_id
          LEFT JOIN experiment_sessions es ON es.id = spr.session_id
         WHERE 1=1
    """
    params = []
    if group_id is not None:
        sql += " AND spr.group_id = ?"
        params.append(int(group_id))
    if session_id is not None:
        sql += " AND spr.session_id = ?"
        params.append(int(session_id))
    if task_id is not None:
        sql += " AND spr.task_id = ?"
        params.append(int(task_id))
    if start_time:
        sql += " AND COALESCE(spr.stage1_started_at, spr.created_at) >= ?"
        params.append(start_time)
    if end_time:
        sql += " AND COALESCE(spr.published_at, spr.updated_at, spr.created_at) <= ?"
        params.append(end_time)
    sql += " ORDER BY spr.group_id ASC, spr.session_id ASC, spr.discussion_id ASC, spr.id ASC"

    rows = []
    for row in query_all(sql, tuple(params)):
        d = dict(row)
        generated_text = d.get("generated_intervention_text") or ""
        validated_text = d.get("validated_intervention_text") or ""
        if blind:
            d["group_condition"] = ""
            generated_text = "[BLINDED AGENT MESSAGE]" if generated_text else HISTORY_NOT_RECORDED
            validated_text = "[BLINDED AGENT MESSAGE]" if validated_text else HISTORY_NOT_RECORDED
        rows.append({
            "export_schema_version": SCHEMA_VERSION,
            "pipeline_run_id": d.get("id"),
            "run_uuid": d.get("run_uuid"),
            "group_id": d.get("group_id"),
            "group_code": d.get("group_code"),
            "group_condition": d.get("group_condition"),
            "session_id": d.get("session_id"),
            "session_no": d.get("export_session_no") or d.get("session_no"),
            "discussion_id": d.get("discussion_id"),
            "task_id": d.get("task_id"),
            "trigger_source": d.get("trigger_source") or HISTORY_NOT_RECORDED,
            "trigger_message_id": d.get("trigger_message_id") or "",
            "trigger_priority": d.get("trigger_priority") if d.get("trigger_priority") is not None else "",
            "input_start_sequence": d.get("input_start_sequence") or "",
            "input_end_sequence": d.get("input_end_sequence") or "",
            "input_cutoff_student_sequence": d.get("input_cutoff_student_sequence") or "",
            "coarse_state_code": d.get("coarse_state_code") or HISTORY_NOT_RECORDED,
            "coarse_decision": d.get("coarse_decision") or HISTORY_NOT_RECORDED,
            "coarse_risk_group": d.get("coarse_risk_group") or HISTORY_NOT_RECORDED,
            "coarse_confidence": d.get("coarse_confidence") if d.get("coarse_confidence") is not None else "",
            "coarse_rule_scores_json": d.get("coarse_rule_scores_json") or "{}",
            "coarse_quantitative_features_json": d.get("coarse_quantitative_features_json") or "{}",
            "coarse_evidence_message_ids_json": d.get("coarse_evidence_message_ids_json") or "[]",
            "coarse_reason_codes_json": d.get("coarse_reason_codes_json") or "[]",
            "raw_sub_state_code": d.get("raw_sub_state_code") or HISTORY_NOT_RECORDED,
            "canonical_sub_state_code": d.get("canonical_sub_state_code") or HISTORY_NOT_RECORDED,
            "canonical_sub_state_label": FINAL_SUB_STATE_LABELS.get(
                d.get("canonical_sub_state_code"),
                HISTORY_NOT_RECORDED,
            ),
            "state_overlays": d.get("secondary_sub_state_tags_json") or "[]",
            "sub_state_confidence": d.get("sub_state_confidence") if d.get("sub_state_confidence") is not None else "",
            "sub_state_start_sequence": d.get("sub_state_start_sequence") or "",
            "sub_state_end_sequence": d.get("sub_state_end_sequence") or "",
            "evidence_message_ids": d.get("sub_state_evidence_message_ids_json") or "[]",
            "should_intervene": d.get("should_intervene") if d.get("should_intervene") is not None else HISTORY_NOT_RECORDED,
            "inhibition_strategy_id": d.get("inhibition_strategy_id") or HISTORY_NOT_RECORDED,
            "inhibition_reason": d.get("inhibition_reason") or HISTORY_NOT_RECORDED,
            "candidate_strategy_ids": d.get("strategy_candidate_ids_json") or "[]",
            "selected_strategy_id": d.get("selected_strategy_id") or HISTORY_NOT_RECORDED,
            "selected_strategy_name": d.get("selected_strategy_name") or HISTORY_NOT_RECORDED,
            "selected_strategy_type": d.get("selected_strategy_type") or HISTORY_NOT_RECORDED,
            "supporting_strategy_ids": d.get("supporting_strategy_ids_json") or "[]",
            "strategy_selection_reason": d.get("strategy_selection_reason") or HISTORY_NOT_RECORDED,
            "strategy_library_version": d.get("strategy_library_version") or HISTORY_NOT_RECORDED,
            "generated_intervention_text": generated_text or HISTORY_NOT_RECORDED,
            "validated_intervention_text": validated_text or HISTORY_NOT_RECORDED,
            "text_validation_result_json": d.get("text_validation_result_json") or HISTORY_NOT_RECORDED,
            "publish_status": d.get("publish_status") or HISTORY_NOT_RECORDED,
            "skip_reason": d.get("skip_reason") or HISTORY_NOT_RECORDED,
            "failure_code": d.get("failure_code") or HISTORY_NOT_RECORDED,
            "failure_detail": d.get("failure_detail") or HISTORY_NOT_RECORDED,
            "published_message_id": d.get("published_message_id") or "",
            "published_at": d.get("published_at") or "",
            "observation_status": d.get("observation_status") or HISTORY_NOT_RECORDED,
            "observation_result": d.get("observation_result") or HISTORY_NOT_RECORDED,
            "observation_previous_sub_state_code": d.get("observation_previous_sub_state_code") or HISTORY_NOT_RECORDED,
            "observation_current_sub_state_code": d.get("observation_current_sub_state_code") or HISTORY_NOT_RECORDED,
            "observation_details_json": d.get("observation_details_json") or "{}",
            "final_status": d.get("final_status") or HISTORY_NOT_RECORDED,
            "created_at": d.get("created_at"),
            "updated_at": d.get("updated_at"),
            "secondary_tags": d.get("secondary_sub_state_tags_json") or "[]",
        })

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def export_interventions_csv(group_id=None, session_id=None, task_id=None,
                              start_time=None, end_time=None, blind=False):
    """Export interventions.csv (intervention_logs)."""
    sql = """SELECT il.*, g.group_code, g.condition AS group_condition, COALESCE(es.session_no, '') AS session_no,
                    ir.id AS intervention_run_id,
                    ir.detected_state AS run_detected_state,
                    ir.confidence AS run_confidence,
                    ir.trigger_type AS run_trigger_type,
                    ir.trigger_reason_json AS run_trigger_reason_json,
                    ir.metadata_json AS run_metadata_json,
                    ir.candidate_strategies AS run_candidate_strategies,
                    ir.selected_strategy AS run_selected_strategy,
                    ir.context_from_sequence AS run_context_from_sequence,
                    ir.context_to_sequence AS run_context_to_sequence,
                    ir.input_message_sequences_json AS run_input_message_sequences_json,
                    ir.evidence_sequences_json AS run_evidence_sequences_json,
                    ir.selected_strategy_id AS run_selected_strategy_id,
                    ir.strategy_pipeline_run_id AS run_strategy_pipeline_run_id,
                    ir.canonical_sub_state_code AS run_canonical_sub_state_code,
                    ir.status AS run_status,
                    ir.publish_status AS run_publish_status,
                    ir.message_id AS run_message_id,
                    ir.discussion_id AS run_discussion_id,
                    ir.generated_message AS run_generated_message,
                    ir.agent_type AS run_agent_type,
                    ir.trigger_type AS run_trigger_source,
                    ir.skip_reason AS run_skip_reason,
                    ir.validation_error AS run_validation_error,
                    ir.actual_started_at AS run_actual_started_at,
                    ir.actual_published_at AS run_actual_published_at,
                    ir.failure_reason AS run_failure_reason,
                    spr.canonical_sub_state_code AS pipeline_canonical_sub_state_code,
                    spr.selected_strategy_id AS pipeline_selected_strategy_id,
                    spr.inhibition_strategy_id AS pipeline_inhibition_strategy_id,
                    spr.should_intervene AS pipeline_should_intervene,
                    spr.failure_code AS pipeline_failure_code,
                    spr.failure_detail AS pipeline_failure_detail,
                    mr.rule_result_json AS monitor_rule_result_json,
                    mr.final_state AS monitor_final_state,
                    mr.review_decision AS monitor_review_decision,
                    mr.review_final_state AS monitor_review_final_state,
                    mr.review_reason AS monitor_review_reason,
                    mr.review_confidence AS monitor_review_confidence
             FROM intervention_logs il
             JOIN groups g ON il.group_id=g.id
             LEFT JOIN intervention_runs ir ON il.intervention_id = ir.id
             LEFT JOIN strategy_pipeline_runs spr
               ON spr.id = ir.strategy_pipeline_run_id
             LEFT JOIN monitor_runs mr ON mr.id = ir.monitor_run_id
             LEFT JOIN experiment_sessions es ON il.session_id = es.id
             WHERE 1=1"""
    params = []
    if group_id is not None:
        sql += " AND il.group_id = ?"
        params.append(int(group_id))
    if session_id is not None:
        sql += " AND il.session_id = ?"
        params.append(int(session_id))
    if task_id is not None:
        sql += " AND il.task_id = ?"
        params.append(int(task_id))
    if start_time:
        sql += " AND il.created_at >= ?"
        params.append(start_time)
    if end_time:
        sql += " AND il.created_at <= ?"
        params.append(end_time)
    sql += " ORDER BY il.id ASC"

    rows = []
    for row in query_all(sql, tuple(params)):
        d = dict(row)
        _apply_intervention_trigger_state(d)
        d["context_from_sequence"] = d.get("run_context_from_sequence")
        d["context_to_sequence"] = d.get("run_context_to_sequence")
        d["input_message_sequences"] = d.get("run_input_message_sequences_json") or ""
        d["evidence_sequences"] = d.get("run_evidence_sequences_json") or ""
        d["rule_candidate_state"] = _review_rule_candidate_state(d)
        d["llm_decision"] = d.get("monitor_review_decision")
        d["llm_final_state"] = d.get("monitor_review_final_state")
        d["llm_reason"] = d.get("monitor_review_reason")
        d["review_confidence"] = d.get("monitor_review_confidence")
        d["agent_type"] = d.get("run_agent_type") or "strategy"
        d["trigger_source"] = d.get("trigger_source") or d.get("run_trigger_source")
        d["strategy_id"] = d.get("strategy_id") or d.get("run_selected_strategy_id")
        canonical_code = (
            d.get("pipeline_canonical_sub_state_code")
            or d.get("run_canonical_sub_state_code")
        )
        if not is_primary_sub_state(canonical_code):
            canonical_code = None
        selected_strategy_id = (
            d.get("pipeline_selected_strategy_id")
            or d.get("run_selected_strategy_id")
            or d.get("strategy_id")
        )
        inhibition_strategy_id = d.get("pipeline_inhibition_strategy_id")
        if str(selected_strategy_id or "").startswith("OI-"):
            inhibition_strategy_id = (
                inhibition_strategy_id or selected_strategy_id
            )
            selected_strategy_id = None
        d["export_schema_version"] = SCHEMA_VERSION
        d["final_sub_state_code"] = canonical_code or ""
        d["canonical_sub_state_code"] = canonical_code or ""
        d["final_sub_state_label"] = (
            FINAL_SUB_STATE_LABELS.get(canonical_code, "")
            if canonical_code
            else ""
        )
        d["strategy_pipeline_run_id"] = (
            d.get("run_strategy_pipeline_run_id") or ""
        )
        d["discussion_id"] = d.get("run_discussion_id") or ""
        d["selected_strategy_id"] = selected_strategy_id or ""
        d["inhibition_strategy_id"] = inhibition_strategy_id or ""
        d["should_intervene"] = (
            d.get("pipeline_should_intervene")
            if d.get("pipeline_should_intervene") is not None
            else ""
        )
        d["failure_code"] = (
            d.get("pipeline_failure_code")
            or d.get("run_validation_error")
            or ""
        )
        d["failure_detail"] = (
            d.get("pipeline_failure_detail")
            or d.get("run_failure_reason")
            or ""
        )
        d["status"] = d.get("run_status") or d.get("status") or ""
        d["publish_status"] = d.get("run_publish_status") or ""
        d["message_id"] = d.get("run_message_id") or ""
        d["assessment_status"] = "confirmed" if canonical_code else ""
        d["assignment_source"] = (
            "strategy_pipeline" if d.get("run_strategy_pipeline_run_id") else ""
        )
        rows.append(d)
    return _rows_to_csv(rows, _interventions_fields(), _interventions_blind, blind,
                         session_id, task_id)


def _interventions_fields():
    return [
        "export_schema_version",
        "id", "group_code", "group_condition", "condition",
        "suggestion_id", "decision_id", "pushed_by_role",
        "push_mode", "trigger_source", "strategy_id", "template_id",
        "sub_category", "strategy_type", "strategy_version",
        "model_name", "prompt_version", "title", "message",
        "session_id", "session_no", "task_id", "intervention_index",
        "discussion_id",
        "final_sub_state_code", "canonical_sub_state_code",
        "final_sub_state_label", "assessment_status", "assignment_source",
        "strategy_pipeline_run_id", "should_intervene",
        "selected_strategy_id", "inhibition_strategy_id",
        "status", "publish_status", "message_id",
        "failure_code", "failure_detail",
        "intervention_run_id", "trigger_state_code", "trigger_state_label",
        "trigger_legacy_state_code", "trigger_normalization_reason",
        "trigger_evidence_tags", "trigger_candidate_scores_json",
        "context_from_sequence", "context_to_sequence",
        "input_message_sequences", "evidence_sequences",
        "rule_candidate_state", "llm_decision", "llm_final_state",
        "llm_reason", "review_confidence", "agent_type",
        "created_at",
    ]


def _interventions_blind(row):
    d = dict(row)
    d["condition"] = ""
    d["group_condition"] = ""
    d["message"] = "[BLINDED]"
    d["template_id"] = None
    d["strategy_id"] = None
    return d


def export_intervention_uptake_csv(group_id=None, session_id=None, task_id=None,
                                    start_time=None, end_time=None, blind=False):
    """Export intervention_uptake.csv."""
    sql = """SELECT iu.*
             FROM intervention_uptake iu
             WHERE 1=1"""
    params = []
    if group_id is not None:
        sql += " AND iu.group_id = ?"
        params.append(int(group_id))
    if session_id is not None:
        sql += " AND iu.session_id = ?"
        params.append(int(session_id))
    if start_time:
        sql += " AND iu.corrected_at >= ?"
        params.append(start_time)
    if end_time:
        sql += " AND iu.corrected_at <= ?"
        params.append(end_time)
    sql += " ORDER BY iu.id ASC"

    rows = query_all(sql, tuple(params))
    fieldnames = [d[1] for d in query_all("PRAGMA table_info('intervention_uptake')")]
    result_rows = []
    for r in rows:
        d = dict(r)
        result_rows.append(d)

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(result_rows)
    return output.getvalue()


def export_process_events_csv(group_id=None, session_id=None, task_id=None,
                               start_time=None, end_time=None, blind=False):
    """Export process_events.csv."""
    sql = """SELECT pe.id, pe.event_type, pe.source,
                    pe.group_id, pe.user_id,
                    COALESCE(pe.participant_code, u.participant_code, '') AS participant_code,
                    COALESCE(ep.display_name, '') AS display_name,
                    COALESCE(pe.group_code, g.group_code, '') AS group_code,
                    COALESCE(g.condition, '') AS condition,
                    """ + _student_group_no_expr("u") + """ AS group_no,
                    """ + _student_member_no_expr("u") + """ AS member_no,
                    pe.related_table, pe.related_id,
                    pe.event_key, pe.actor_role,
                    pe.session_id, pe.session_no, pe.task_id,
                    COALESCE(pe.payload, '') AS payload,
                    pe.created_at
             FROM process_events pe
             LEFT JOIN users u ON pe.user_id = u.id
             LEFT JOIN groups g ON pe.group_id = g.id
             LEFT JOIN experiment_participants ep ON pe.user_id = ep.user_id AND pe.group_id = ep.group_id
             WHERE 1=1"""
    params = []
    if group_id is not None:
        sql += " AND pe.group_id = ?"
        params.append(int(group_id))
    if session_id is not None:
        sql += " AND pe.session_id = ?"
        params.append(int(session_id))
    if task_id is not None:
        sql += " AND pe.task_id = ?"
        params.append(int(task_id))
    if start_time:
        sql += " AND pe.created_at >= ?"
        params.append(start_time)
    if end_time:
        sql += " AND pe.created_at <= ?"
        params.append(end_time)
    sql += " ORDER BY pe.id ASC"

    rows = query_all(sql, tuple(params))
    fieldnames = [
        "id", "event_type", "source", "group_id", "user_id",
        "participant_code", "display_name", "group_code", "condition",
        "group_no", "member_no",
        "related_table", "related_id", "event_key", "actor_role",
        "session_id", "session_no", "task_id", "payload", "created_at",
    ]
    result_rows = []
    for r in rows:
        d = dict(r)
        if blind:
            d["condition"] = ""
        result_rows.append(d)

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(result_rows)
    return output.getvalue()


def export_ssrl_events_csv(group_id=None, session_id=None, task_id=None,
                            start_time=None, end_time=None, blind=False):
    """Export ssrl_events.csv (autonomous_regulation_events)."""
    sql = """SELECT are.*
             FROM autonomous_regulation_events are
             WHERE 1=1"""
    params = []
    if group_id is not None:
        sql += " AND are.group_id = ?"
        params.append(int(group_id))
    if session_id is not None:
        sql += " AND are.session_id = ?"
        params.append(int(session_id))
    if task_id is not None:
        sql += " AND are.task_id = ?"
        params.append(int(task_id))
    if start_time:
        sql += " AND are.created_at >= ?"
        params.append(start_time)
    if end_time:
        sql += " AND are.created_at <= ?"
        params.append(end_time)
    sql += " ORDER BY are.id ASC"

    rows = query_all(sql, tuple(params))
    fieldnames = [d["name"] for d in query_all("PRAGMA table_info('autonomous_regulation_events')")]
    result_rows = []
    for r in rows:
        d = dict(r)
        result_rows.append(d)

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(result_rows)
    return output.getvalue()


def export_autonomous_regulation_events_csv(group_id=None, session_id=None, task_id=None,
                                              start_time=None, end_time=None, blind=False):
    """Alias: export autonomous_regulation_events.csv same as ssrl_events."""
    return export_ssrl_events_csv(group_id, session_id, task_id,
                                   start_time, end_time, blind)


def _classify_regulation_event(event_type):
    text = (event_type or "").lower()
    if "ssrl" in text or "self_regulation" in text or "self-regulation" in text:
        return "ssrl"
    return "autonomous_regulation"


def export_unified_events_csv(group_id=None, session_id=None, task_id=None,
                              start_time=None, end_time=None, blind=False):
    """Export a single merged event stream.

    Merges process_events and autonomous_regulation_events, with explicit
    category columns so downstream analysis can distinguish event families.
    """
    fieldnames = [
        "export_schema_version", "event_category", "event_type",
        "source_export_name", "source_table", "event_id",
        "event_subtype", "event_key", "event_time", "source",
        "group_id", "group_code", "group_no", "condition",
        "user_id", "participant_code", "display_name", "member_no", "actor_role",
        "session_id", "session_no", "discussion_id", "task_id",
        "related_table", "related_id",
        "display_state_code", "display_state_label",
        "final_sub_state_code", "final_sub_state_label",
        "coarse_state_code", "assignment_source", "assessment_status",
        "state_assignment_reason", "inferred", "segment_id",
        "assessment_batch_id", "strategy_pipeline_run_id",
        "selected_strategy_id", "inhibition_strategy_id",
        "error_code", "failure_detail", "start_at", "end_at",
        "state_code", "state_label", "legacy_state_code",
        "normalization_reason", "evidence_tags", "candidate_scores_json",
        "state_system_version",
        "confidence", "detected_by", "evidence_message_ids_json",
        "note", "metadata_json", "payload",
    ]

    rows = []

    process_sql = """SELECT pe.id, pe.event_type, pe.source,
                            pe.group_id, pe.user_id,
                            COALESCE(pe.participant_code, u.participant_code, '') AS participant_code,
                            COALESCE(ep.display_name, '') AS display_name,
                            COALESCE(pe.group_code, g.group_code, '') AS group_code,
                            COALESCE(g.condition, '') AS condition,
                            """ + _student_group_no_expr("u") + """ AS group_no,
                            """ + _student_member_no_expr("u") + """ AS member_no,
                            pe.related_table, pe.related_id,
                            pe.event_key, pe.actor_role,
                            pe.session_id, pe.session_no, pe.task_id,
                            COALESCE(pe.payload, '') AS payload,
                            pe.created_at
                     FROM process_events pe
                     LEFT JOIN users u ON pe.user_id = u.id
                     LEFT JOIN groups g ON pe.group_id = g.id
                     LEFT JOIN experiment_participants ep
                       ON pe.user_id = ep.user_id AND pe.group_id = ep.group_id
                     WHERE 1=1"""
    process_params = []
    if group_id is not None:
        process_sql += " AND pe.group_id = ?"
        process_params.append(int(group_id))
    if session_id is not None:
        process_sql += " AND pe.session_id = ?"
        process_params.append(int(session_id))
    if task_id is not None:
        process_sql += " AND pe.task_id = ?"
        process_params.append(int(task_id))
    if start_time:
        process_sql += " AND pe.created_at >= ?"
        process_params.append(start_time)
    if end_time:
        process_sql += " AND pe.created_at <= ?"
        process_params.append(end_time)
    for r in query_all(process_sql + " ORDER BY pe.created_at ASC, pe.id ASC", tuple(process_params)):
        d = dict(r)
        if blind:
            d["condition"] = ""
        group_code = d.get("group_code") or ""
        row = {
            "export_schema_version": SCHEMA_VERSION,
            "event_category": "process",
            "event_type": d.get("event_type"),
            "source_export_name": "process_events.csv",
            "source_table": "process_events",
            "event_id": d.get("id"),
            "event_subtype": d.get("event_type"),
            "event_key": d.get("event_key"),
            "event_time": d.get("created_at"),
            "source": d.get("source"),
            "group_id": d.get("group_id"),
            "group_code": group_code,
            "group_no": d.get("group_no") or _group_no_from_code(group_code),
            "condition": d.get("condition"),
            "user_id": d.get("user_id"),
            "participant_code": d.get("participant_code"),
            "display_name": d.get("display_name"),
            "member_no": d.get("member_no"),
            "actor_role": d.get("actor_role"),
            "session_id": d.get("session_id"),
            "session_no": d.get("session_no"),
            "task_id": d.get("task_id"),
            "related_table": d.get("related_table"),
            "related_id": d.get("related_id"),
            "payload": d.get("payload"),
        }
        rows.append(row)

    for d in _load_message_export_records(
        group_id=group_id,
        session_id=session_id,
        task_id=task_id,
        start_time=start_time,
        end_time=end_time,
    ):
        if str(d.get("role") or "").strip().lower() != "student":
            continue
        if blind:
            d["condition"] = ""
        final_code = d.get("final_sub_state_code") or ""
        payload = {
            "message_id": d.get("message_id"),
            "sequence": d.get("sequence"),
            "display_state_code": d.get("display_state_code"),
            "display_state_label": d.get("display_state_label"),
            "assignment_source": d.get("assignment_source"),
            "assessment_status": d.get("assessment_status"),
            "state_assignment_reason": d.get("state_assignment_reason"),
            "inferred": bool(d.get("inferred")),
            "error_code": d.get("error_code"),
        }
        rows.append({
            "export_schema_version": SCHEMA_VERSION,
            "event_category": "state",
            "event_type": "state_assignment",
            "source_export_name": "messages.csv",
            "source_table": "messages",
            "event_id": d.get("message_id"),
            "event_subtype": "state_assignment",
            "event_key": "state_assignment:%s" % d.get("message_id"),
            "event_time": d.get("created_at"),
            "source": d.get("assignment_source"),
            "group_id": d.get("group_id"),
            "group_code": d.get("group_code"),
            "group_no": d.get("group_no"),
            "condition": d.get("condition"),
            "user_id": d.get("user_id"),
            "participant_code": d.get("participant_code"),
            "display_name": d.get("display_name"),
            "member_no": d.get("member_no"),
            "actor_role": "student",
            "session_id": d.get("session_id"),
            "session_no": d.get("session_no"),
            "discussion_id": d.get("discussion_id"),
            "task_id": d.get("task_id"),
            "related_table": "messages",
            "related_id": d.get("message_id"),
            "display_state_code": d.get("display_state_code"),
            "display_state_label": d.get("display_state_label"),
            "final_sub_state_code": final_code,
            "final_sub_state_label": d.get("final_sub_state_label"),
            "coarse_state_code": d.get("coarse_state_code"),
            "assignment_source": d.get("assignment_source"),
            "assessment_status": d.get("assessment_status"),
            "state_assignment_reason": d.get("state_assignment_reason"),
            "inferred": d.get("inferred"),
            "segment_id": d.get("segment_id"),
            "assessment_batch_id": d.get("assessment_batch_id"),
            "strategy_pipeline_run_id": d.get("strategy_pipeline_run_id"),
            "selected_strategy_id": d.get("selected_strategy_id"),
            "inhibition_strategy_id": d.get("inhibition_strategy_id"),
            "error_code": d.get("error_code"),
            "failure_detail": d.get("failure_detail"),
            # Compatibility projection; primary research analysis must use
            # final_sub_state_code.
            "state_code": final_code,
            "state_label": d.get("final_sub_state_label"),
            "legacy_state_code": d.get("legacy_state_code"),
            "confidence": d.get("confidence"),
            "state_system_version": STATE_EXPORT_SYSTEM_VERSION,
            "payload": _json_dumps_cell(payload),
        })

    state_sql = """SELECT sa.*, g.group_code, g.condition,
                          COALESCE(es.session_no, sa.session_no, '') AS export_session_no
                   FROM state_assessments sa
                   LEFT JOIN groups g ON sa.group_id = g.id
                   LEFT JOIN experiment_sessions es ON sa.session_id = es.id
                   WHERE 1=1"""
    state_params = []
    if group_id is not None:
        state_sql += " AND sa.group_id = ?"
        state_params.append(int(group_id))
    if session_id is not None:
        state_sql += " AND sa.session_id = ?"
        state_params.append(int(session_id))
    if task_id is not None:
        state_sql += " AND sa.task_id = ?"
        state_params.append(int(task_id))
    if start_time:
        state_sql += " AND sa.created_at >= ?"
        state_params.append(start_time)
    if end_time:
        state_sql += " AND sa.created_at <= ?"
        state_params.append(end_time)
    for r in query_all(state_sql + " ORDER BY sa.created_at ASC, sa.id ASC", tuple(state_params)):
        d = _assessment_export_record(dict(r))
        if blind:
            d["condition"] = ""
        payload = {
            "raw_state_code": d.get("raw_state_code"),
            "coarse_state_code": d.get("coarse_state_code"),
            "rule_state_code": d.get("rule_state_code"),
            "llm_coarse_state_code": d.get("llm_coarse_state_code"),
            "fused_coarse_state_code": d.get("fused_coarse_state_code"),
            "legacy_state_code": d.get("legacy_state_code"),
            "normalization_reason": d.get("normalization_reason"),
            "window_start": d.get("window_start"),
            "window_end": d.get("window_end"),
            "assessment_status": d.get("assessment_status"),
            "state_system_version": d.get("state_system_version"),
        }
        group_code = d.get("group_code") or ""
        row = {
            "export_schema_version": SCHEMA_VERSION,
            "event_category": "detector_coarse",
            "event_type": "detector_coarse_assessment",
            "source_export_name": "detector_outputs.csv",
            "source_table": "state_assessments",
            "event_id": d.get("id"),
            "event_subtype": "detector_coarse_assessment",
            "event_key": "detector_coarse_assessment:%s" % d.get("id"),
            "event_time": d.get("created_at") or d.get("window_end") or d.get("window_start"),
            "source": d.get("detector_version") or d.get("model_name") or "state_monitoring",
            "group_id": d.get("group_id"),
            "group_code": group_code,
            "group_no": _group_no_from_code(group_code),
            "condition": d.get("condition"),
            "session_id": d.get("session_id"),
            "session_no": d.get("export_session_no") or d.get("session_no"),
            "discussion_id": d.get("discussion_id"),
            "task_id": d.get("task_id"),
            "related_table": "state_assessments",
            "related_id": d.get("id"),
            "coarse_state_code": d.get("coarse_state_code"),
            "assessment_status": d.get("assessment_status"),
            "state_code": d.get("coarse_state_code"),
            "state_label": d.get("coarse_state_label"),
            "legacy_state_code": d.get("legacy_state_code"),
            "normalization_reason": d.get("normalization_reason"),
            "evidence_tags": d.get("evidence_tags"),
            "candidate_scores_json": d.get("candidate_scores_json"),
            "state_system_version": d.get("state_system_version"),
            "confidence": d.get("confidence"),
            "detected_by": d.get("detector_version") or d.get("model_name"),
            "metadata_json": d.get("fusion_json"),
            "payload": _json_dumps_cell(payload),
        }
        rows.append(row)

    silence_sql = """
        SELECT s.*, g.group_code, g.condition,
               COALESCE(es.session_no, s.session_no, '') AS export_session_no
          FROM collaboration_state_segments s
          LEFT JOIN groups g ON g.id=s.group_id
          LEFT JOIN experiment_sessions es ON es.id=s.session_id
         WHERE s.segment_kind='time_range'
           AND COALESCE(s.coarse_state_code, s.state_code)='negative_silence'
    """
    silence_params = []
    if group_id is not None:
        silence_sql += " AND s.group_id=?"
        silence_params.append(int(group_id))
    if session_id is not None:
        silence_sql += " AND s.session_id=?"
        silence_params.append(int(session_id))
    if task_id is not None:
        silence_sql += " AND s.task_id=?"
        silence_params.append(int(task_id))
    if start_time:
        silence_sql += (
            " AND COALESCE(s.end_at, s.last_observed_at, s.created_at)>=?"
        )
        silence_params.append(start_time)
    if end_time:
        silence_sql += " AND s.start_at<=?"
        silence_params.append(end_time)
    silence_sql += " ORDER BY s.start_at ASC, s.id ASC"
    for raw in query_all(silence_sql, tuple(silence_params)):
        d = dict(raw)
        if blind:
            d["condition"] = ""
        group_code = d.get("group_code") or ""
        payload = {
            "segment_id": d.get("id"),
            "start_at": d.get("start_at"),
            "end_at": d.get("end_at"),
            "last_observed_at": d.get("last_observed_at"),
            "gap_seconds": d.get("gap_seconds"),
            "is_active": bool(d.get("is_active")),
            "resolution_reason": d.get("resolution_reason"),
        }
        rows.append({
            "export_schema_version": SCHEMA_VERSION,
            "event_category": "state",
            "event_type": "silence_time_range",
            "source_export_name": "unified-events.csv",
            "source_table": "collaboration_state_segments",
            "event_id": d.get("id"),
            "event_subtype": "silence_time_range",
            "event_key": d.get("silence_event_key") or "silence:%s" % d.get("id"),
            "event_time": d.get("start_at"),
            "source": d.get("source") or "silence_rule",
            "group_id": d.get("group_id"),
            "group_code": group_code,
            "group_no": _group_no_from_code(group_code),
            "condition": d.get("condition"),
            "session_id": d.get("session_id"),
            "session_no": d.get("export_session_no") or d.get("session_no"),
            "discussion_id": d.get("discussion_id"),
            "task_id": d.get("task_id"),
            "related_table": "collaboration_state_segments",
            "related_id": d.get("id"),
            "coarse_state_code": "negative_silence",
            "assignment_source": d.get("source") or "silence_rule",
            "segment_id": d.get("id"),
            "start_at": d.get("start_at"),
            "end_at": d.get("end_at") or d.get("last_observed_at"),
            "state_code": "negative_silence",
            "state_label": "消极沉默",
            "legacy_state_code": "negative_silence",
            "state_system_version": STATE_EXPORT_SYSTEM_VERSION,
            "payload": _json_dumps_cell(payload),
        })

    reg_sql = """SELECT are.*, g.group_code, g.condition,
                        COALESCE(es.session_no, '') AS session_no
                 FROM autonomous_regulation_events are
                 LEFT JOIN groups g ON are.group_id = g.id
                 LEFT JOIN experiment_sessions es ON are.session_id = es.id
                 WHERE 1=1"""
    reg_params = []
    if group_id is not None:
        reg_sql += " AND are.group_id = ?"
        reg_params.append(int(group_id))
    if session_id is not None:
        reg_sql += " AND are.session_id = ?"
        reg_params.append(int(session_id))
    if task_id is not None:
        reg_sql += " AND are.task_id = ?"
        reg_params.append(int(task_id))
    if start_time:
        reg_sql += " AND are.created_at >= ?"
        reg_params.append(start_time)
    if end_time:
        reg_sql += " AND are.created_at <= ?"
        reg_params.append(end_time)
    for r in query_all(reg_sql + " ORDER BY are.created_at ASC, are.id ASC", tuple(reg_params)):
        d = dict(r)
        if blind:
            d["condition"] = ""
        category = _classify_regulation_event(d.get("event_type"))
        source_export = (
            "ssrl_events.csv" if category == "ssrl"
            else "autonomous_regulation_events.csv"
        )
        group_code = d.get("group_code") or ""
        payload = {
            "metadata_json": d.get("metadata_json"),
            "note": d.get("note"),
            "source_monitor_run_id": d.get("source_monitor_run_id"),
        }
        row = {
            "export_schema_version": SCHEMA_VERSION,
            "event_category": category,
            "event_type": d.get("event_type"),
            "source_export_name": source_export,
            "source_table": "autonomous_regulation_events",
            "event_id": d.get("id"),
            "event_subtype": d.get("event_type"),
            "event_time": d.get("created_at"),
            "source": d.get("detected_by"),
            "group_id": d.get("group_id"),
            "group_code": group_code,
            "group_no": _group_no_from_code(group_code),
            "condition": d.get("condition"),
            "session_id": d.get("session_id"),
            "session_no": d.get("session_no"),
            "task_id": d.get("task_id"),
            "confidence": d.get("confidence"),
            "detected_by": d.get("detected_by"),
            "evidence_message_ids_json": d.get("evidence_message_ids_json"),
            "note": d.get("note"),
            "metadata_json": d.get("metadata_json"),
            "payload": _json_dumps_cell(payload),
        }
        rows.append(row)

    rows.sort(key=lambda row: (row.get("event_time") or "", str(row.get("source_table") or ""), int(row.get("event_id") or 0)))

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def export_participation_summary_csv(group_id=None, session_id=None, task_id=None,
                                      start_time=None, end_time=None, blind=False):
    """Export participation_summary.csv (aggregated from messages)."""
    sql = """SELECT u.participant_code, g.group_code, g.condition,
                    u.id AS user_id, g.id AS group_id,
                    COUNT(m.id) AS message_count,
                    COALESCE(SUM(LENGTH(m.content)), 0) AS char_count,
                    MIN(m.created_at) AS first_msg_at,
                    MAX(m.created_at) AS last_msg_at
             FROM messages m
             JOIN users u ON m.user_id=u.id
             JOIN groups g ON m.group_id=g.id
             WHERE u.role='student'
               AND m.role='student'"""
    params = []
    if group_id is not None:
        sql += " AND m.group_id = ?"
        params.append(int(group_id))
    if session_id is not None:
        sql += " AND m.session_id = ?"
        params.append(int(session_id))
    if start_time:
        sql += " AND m.created_at >= ?"
        params.append(start_time)
    if end_time:
        sql += " AND m.created_at <= ?"
        params.append(end_time)
    sql += " GROUP BY u.id, g.id ORDER BY g.id, u.id ASC"

    rows = query_all(sql, tuple(params))
    fieldnames = ["participant_code", "group_code", "condition",
                   "message_count", "char_count", "first_msg_at", "last_msg_at"]

    result_rows = []
    for r in rows:
        d = dict(r)
        if blind:
            d["condition"] = ""
            d.pop("user_id", None)
        result_rows.append(d)

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(result_rows)
    return output.getvalue()


def export_emotion_snapshots_csv(group_id=None, session_id=None, task_id=None,
                                  start_time=None, end_time=None, blind=False):
    """Export emotion_snapshots.csv (emotion_checkins)."""
    sql = """SELECT e.id, u.participant_code,
                    COALESCE(ep.display_name, '') AS display_name,
                    g.group_code, g.condition,
                    """ + _student_group_no_expr("u") + """ AS group_no,
                    """ + _student_member_no_expr("u") + """ AS member_no,
                    e.emotion_option, e.positivity, e.engagement, e.atmosphere,
                    e.expression_willingness,
                    COALESCE(e.checkin_type, 'post') AS checkin_type,
                    e.note, e.created_at, e.session_id, es.session_no, e.task_id
             FROM emotion_checkins e
             JOIN groups g ON e.group_id=g.id
             JOIN users u ON e.user_id=u.id
             LEFT JOIN experiment_participants ep ON e.user_id=ep.user_id AND e.group_id=ep.group_id
             LEFT JOIN experiment_sessions es ON e.session_id = es.id
             WHERE 1=1"""
    params = []
    if group_id is not None:
        sql += " AND e.group_id = ?"
        params.append(int(group_id))
    if session_id is not None:
        sql += " AND e.session_id = ?"
        params.append(int(session_id))
    if start_time:
        sql += " AND e.created_at >= ?"
        params.append(start_time)
    if end_time:
        sql += " AND e.created_at <= ?"
        params.append(end_time)
    sql += " ORDER BY e.id ASC"

    rows = query_all(sql, tuple(params))
    return _rows_to_csv(rows, _emotion_fields(), _emotion_blind, blind,
                         session_id, task_id)


def _emotion_fields():
    return [
        "id", "participant_code", "display_name", "group_code", "condition",
        "group_no", "member_no",
        "emotion_option", "positivity", "engagement", "atmosphere",
        "expression_willingness", "checkin_type", "note", "created_at",
        "session_id", "session_no", "task_id",
    ]


def _emotion_blind(row):
    d = dict(row)
    d["condition"] = ""
    return d


def export_deliverables_csv(group_id=None, session_id=None, task_id=None,
                             start_time=None, end_time=None, blind=False):
    """Export deliverables.csv (collaborative_documents)."""
    sql = """SELECT cd.*, g.group_code, COALESCE(lt.title, '') AS task_title
             FROM collaborative_documents cd
             JOIN groups g ON cd.group_id=g.id
             LEFT JOIN learning_tasks lt ON cd.task_id = lt.id
             WHERE cd.status='submitted'"""
    params = []
    if group_id is not None:
        sql += " AND cd.group_id = ?"
        params.append(int(group_id))
    if session_id is not None:
        sql += " AND cd.session_id = ?"
        params.append(int(session_id))
    if task_id is not None:
        sql += " AND cd.task_id = ?"
        params.append(int(task_id))
    if start_time:
        sql += " AND cd.submitted_at >= ?"
        params.append(start_time)
    if end_time:
        sql += " AND cd.submitted_at <= ?"
        params.append(end_time)
    sql += " ORDER BY cd.id ASC"

    rows = query_all(sql, tuple(params))
    fieldnames = [d["name"] for d in query_all("PRAGMA table_info('collaborative_documents')")]
    fieldnames += ["group_code", "task_title"]
    result_rows = []
    for r in rows:
        d = dict(r)
        if blind:
            d.pop("condition", None)
            d["content_html"] = None
        result_rows.append(d)

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(result_rows)
    return output.getvalue()



def export_survey_responses_csv(group_id=None, session_id=None, task_id=None,
                                 start_time=None, end_time=None, blind=False):
    """Export survey_responses.csv."""
    sql = """SELECT qr.id, u.participant_code,
                    COALESCE(ep.display_name, '') AS display_name,
                    g.group_code,
                    """ + _student_group_no_expr("u") + """ AS group_no,
                    """ + _student_member_no_expr("u") + """ AS member_no,
                    qr.session_no, qr.task_id, qr.session_id,
                    q.id AS questionnaire_id,
                    q.code AS questionnaire_code, q.category_key,
                    q.title AS questionnaire_title, q.timing AS questionnaire_timing,
                    q.scale_max, qi.item_code, qi.question_type,
                    qi.dimension_label, qi.prompt_text,
                    qr.response_stage, qr.response_value, qr.response_text,
                    qr.response_option_key,
                    COALESCE(NULLIF(qr.response_option_key, ''),
                             NULLIF(qr.response_text, ''),
                             CAST(qr.response_value AS TEXT)) AS raw_answer,
                    qr.response_batch_id,
                    qr.created_at
             FROM questionnaire_responses qr
             JOIN questionnaires q ON qr.questionnaire_id=q.id
             JOIN questionnaire_items qi ON qr.item_id=qi.id
             JOIN users u ON qr.user_id=u.id
             LEFT JOIN experiment_participants ep ON qr.user_id=ep.user_id AND qr.group_id=ep.group_id
             LEFT JOIN groups g ON qr.group_id=g.id
             WHERE 1=1"""
    params = []
    if group_id is not None:
        sql += " AND qr.group_id = ?"
        params.append(int(group_id))
    if session_id is not None:
        sql += " AND qr.session_id = ?"
        params.append(int(session_id))
    if task_id is not None:
        sql += " AND qr.task_id = ?"
        params.append(int(task_id))
    if start_time:
        sql += " AND qr.created_at >= ?"
        params.append(start_time)
    if end_time:
        sql += " AND qr.created_at <= ?"
        params.append(end_time)
    sql += " ORDER BY qr.id ASC"

    rows = query_all(sql, tuple(params))
    return _rows_to_csv(rows, _survey_fields(), _survey_blind, blind,
                         session_id, task_id)


def _survey_fields():
    return [
        "id", "participant_code", "display_name", "group_code",
        "group_no", "member_no",
        "session_no", "task_id", "session_id",
        "questionnaire_id",
        "questionnaire_code", "category_key", "questionnaire_title",
        "questionnaire_timing", "scale_max",
        "item_code", "question_type", "dimension_label", "prompt_text",
        "response_stage", "response_value", "response_text",
        "response_option_key", "raw_answer", "response_batch_id",
        "created_at",
    ]


def _survey_blind(row):
    return dict(row)



def export_help_requests_csv(group_id=None, session_id=None, task_id=None,
                              start_time=None, end_time=None, blind=False):
    """Export help_requests.csv."""
    sql = """SELECT hr.id, u.participant_code,
                    COALESCE(ep.display_name, '') AS display_name,
                    g.group_code, g.condition,
                    """ + _student_group_no_expr("u") + """ AS group_no,
                    """ + _student_member_no_expr("u") + """ AS member_no,
                    hr.session_id, hr.session_no, hr.task_id,
                    hr.status, hr.request_text, hr.intent, hr.response_message,
                    hr.fallback_used, hr.source_message_id, hr.response_message_id,
                    hr.intervention_run_id,
                    hr.failure_reason, hr.created_at, hr.completed_at
             FROM help_requests hr
             JOIN groups g ON hr.group_id=g.id
             JOIN users u ON hr.requester_id=u.id
             LEFT JOIN experiment_participants ep ON hr.requester_id=ep.user_id AND hr.group_id=ep.group_id
             WHERE 1=1"""
    params = []
    if group_id is not None:
        sql += " AND hr.group_id = ?"
        params.append(int(group_id))
    if session_id is not None:
        sql += " AND hr.session_id = ?"
        params.append(int(session_id))
    if task_id is not None:
        sql += " AND hr.task_id = ?"
        params.append(int(task_id))
    if start_time:
        sql += " AND hr.created_at >= ?"
        params.append(start_time)
    if end_time:
        sql += " AND hr.created_at <= ?"
        params.append(end_time)
    sql += " ORDER BY hr.id ASC"

    rows = query_all(sql, tuple(params))
    return _rows_to_csv(rows, _help_requests_fields(), _help_requests_blind, blind,
                         session_id, task_id)


def _help_requests_fields():
    return [
        "id", "participant_code", "display_name", "group_code", "condition",
        "group_no", "member_no",
        "session_id", "session_no", "task_id",
        "status", "request_text", "intent", "response_message",
        "fallback_used", "source_message_id", "response_message_id", "intervention_run_id",
        "failure_reason", "created_at", "completed_at",
    ]


def _help_requests_blind(row):
    d = dict(row)
    d["condition"] = ""
    d["response_message"] = "[BLINDED]"
    return d



def export_audit_logs_csv(group_id=None, session_id=None, task_id=None,
                           start_time=None, end_time=None, blind=False):
    """Export audit_logs.csv."""
    sql = """SELECT al.*
             FROM audit_logs al
             WHERE 1=1"""
    params = []
    if start_time:
        sql += " AND al.created_at >= ?"
        params.append(start_time)
    if end_time:
        sql += " AND al.created_at <= ?"
        params.append(end_time)
    sql += " ORDER BY al.id ASC"

    rows = query_all(sql, tuple(params))
    fieldnames = [d[1] for d in query_all("PRAGMA table_info('audit_logs')")]
    result_rows = []
    for r in rows:
        d = dict(r)
        result_rows.append(d)

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(result_rows)
    return output.getvalue()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rows_to_csv(rows, fieldnames, blind_fn, blind, session_id, task_id,
                 prepared_rows=None):
    """Convert DB rows to CSV string using a field list and blind handler."""
    result_rows = []
    for r in (prepared_rows if prepared_rows is not None else rows):
        d = blind_fn(dict(r)) if blind else dict(r)
        result_rows.append(d)

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(result_rows)
    return output.getvalue()


def _blur_agent_content(role, content):
    """Blur agent message content in blind mode."""
    if role and str(role).lower() == "agent":
        return "[BLINDED AGENT MESSAGE]"
    return content


# ---------------------------------------------------------------------------
# Export registry: maps filename -> (function, requires_intervention_effect_flag)
# ---------------------------------------------------------------------------

EXPORT_REGISTRY = {
    "messages.csv": (export_messages_csv, False),
    "detector_outputs.csv": (export_detector_outputs_csv, False),
    "strategy_pipeline_runs.csv": (export_strategy_pipeline_runs_csv, False),
    "interventions.csv": (export_interventions_csv, False),
    "participation_summary.csv": (export_participation_summary_csv, False),
    "emotion_snapshots.csv": (export_emotion_snapshots_csv, False),
    "deliverables.csv": (export_deliverables_csv, False),
    "survey_responses.csv": (export_survey_responses_csv, False),
    "help_requests.csv": (export_help_requests_csv, False),
}
