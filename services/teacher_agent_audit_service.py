# -*- coding: utf-8 -*-
"""T5: Teacher agent audit service.

Provides the full audit chain query (detector -> gate -> intervention -> uptake -> behavior change),
manual uptake correction, and unblind recording for the T5 Agent Audit page.

All SQL is parameterised. Blind mode hides condition, library name, and raw agent text.
"""
import json
from datetime import datetime
from db import db, query_all, query_one, execute, now_str, write_audit_log
from knowledge_base import normalize_state_payload
from services.three_stage_schema import (
    FINAL_SUB_STATE_CODES,
    FINAL_SUB_STATE_LABELS,
    LEGACY_STATE_CODES,
    LEGACY_STATE_LABELS,
    PROCESS_STATE_LABELS,
    is_primary_sub_state,
    normalize_final_sub_state,
)

# Valid manual uptake types per constraint 7.
VALID_UPTAKE_TYPES = frozenset({
    "ignored", "acknowledged", "discussed", "adopted", "adapted", "rejected"
})

HISTORY_NOT_RECORDED = "历史数据未记录"


def _table_columns(table_name):
    try:
        return [row["name"] for row in query_all("PRAGMA table_info('%s')" % table_name)]
    except Exception:
        return []


def _table_exists(table_name):
    return bool(_table_columns(table_name))


def _safe_json(value):
    if not value:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _int_list(value):
    parsed = _safe_json(value) if isinstance(value, str) else value
    if parsed in (None, ""):
        return []
    if not isinstance(parsed, (list, tuple, set)):
        parsed = [parsed]
    result = []
    for item in parsed:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number not in result:
            result.append(number)
    return result


def _first_value(*values):
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _boolish(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "y", "pass", "passed", "ok", "intervene"):
        return True
    if text in ("0", "false", "no", "n", "reject", "rejected", "skip", "skipped"):
        return False
    return value


def _merge_tags(target, value):
    if not value:
        return
    if isinstance(value, dict):
        iterable = list(value.keys()) + list(value.values())
    elif isinstance(value, (list, tuple, set)):
        iterable = value
    else:
        iterable = str(value).replace("，", ",").replace("；", ",").replace(";", ",").split(",")
    for item in iterable:
        text = str(item or "").strip()
        if text and text not in target:
            target.append(text)


def _tags_from_evidence_text(text):
    if not text or "evidence_tags=" not in str(text):
        return []
    fragment = str(text).split("evidence_tags=", 1)[1]
    fragment = fragment.split(";", 1)[0]
    tags = []
    _merge_tags(tags, fragment)
    return tags


def _collect_evidence_tags(*payloads):
    tags = []
    for payload in payloads:
        if not payload:
            continue
        if isinstance(payload, dict):
            for key in ("evidence_tags", "secondary_flags", "tags"):
                _merge_tags(tags, payload.get(key))
            signals = payload.get("signals")
            if isinstance(signals, dict):
                _merge_tags(tags, signals.get("evidence_tags"))
                _merge_tags(tags, signals.get("coordination_evidence_tags"))
            candidates = payload.get("candidates")
            if isinstance(candidates, list):
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        continue
                    for signal in candidate.get("signals") or []:
                        if isinstance(signal, dict):
                            _merge_tags(tags, signal.get("reason"))
            result = payload.get("result")
            if isinstance(result, dict):
                _merge_tags(tags, result.get("evidence_tags"))
        else:
            _merge_tags(tags, _tags_from_evidence_text(payload))
    return tags


def _merge_score(target, key, value):
    if key is None or value is None:
        return
    try:
        score = round(float(value), 3)
    except (TypeError, ValueError):
        return
    target[str(key)] = score


def _merge_score_map(target, value):
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, dict):
                _merge_score(target, key, item.get("score") or item.get("confidence") or item.get("value"))
            else:
                _merge_score(target, key, item)
    elif isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            key = item.get("state_code") or item.get("state") or item.get("code")
            _merge_score(target, key, item.get("score") or item.get("confidence") or item.get("value"))


def _collect_candidate_scores(*payloads, state_score=None, confidence=None):
    scores = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in ("candidate_scores", "state_scores", "scores", "score_by_state", "candidates"):
            _merge_score_map(scores, payload.get(key))
        result = payload.get("result")
        if isinstance(result, dict):
            _merge_score(scores, result.get("state_code") or result.get("primary_state"), result.get("confidence"))
        if payload.get("rule_state_code"):
            _merge_score(scores, payload.get("rule_state_code"), payload.get("rule_score"))
        if payload.get("llm_state_code"):
            _merge_score(scores, payload.get("llm_state_code"), payload.get("llm_confidence"))
        if payload.get("fused_state_code"):
            _merge_score(scores, payload.get("fused_state_code"), payload.get("confidence"))
    if state_score is not None:
        _merge_score(scores, "rule_state_score", state_score)
    if confidence is not None:
        _merge_score(scores, "final_confidence", confidence)
    return scores


def _normalize_audit_state(row):
    raw_state = (
        row.get("fused_state_code")
        or row.get("state_code")
        or row.get("collaboration_state")
        or row.get("rule_state_code")
        or row.get("llm_state_code")
        or "unknown"
    )
    if row.get("assessment_status") == "insufficient_evidence":
        raw_state = "unknown"
    rule_json = _safe_json(row.get("rule_result_json")) or _safe_json(row.get("rule_assessment_json")) or {}
    llm_json = _safe_json(row.get("llm_result_json")) or _safe_json(row.get("llm_assessment_json")) or {}
    fusion_json = _safe_json(row.get("fusion_json")) or {}
    evidence_tags = _collect_evidence_tags(
        row.get("evidence"),
        row.get("evidence_summary"),
        rule_json,
        llm_json,
        fusion_json,
    )
    normalized = normalize_state_payload(
        raw_state,
        evidence_tags=evidence_tags,
        assessment_status=row.get("assessment_status"),
    )
    candidate_scores = _collect_candidate_scores(
        rule_json,
        llm_json,
        fusion_json,
        state_score=row.get("state_score"),
        confidence=row.get("confidence"),
    )
    llm_result = llm_json.get("result") if isinstance(llm_json.get("result"), dict) else {}
    normalization_reason = (
        fusion_json.get("normalization_reason")
        or rule_json.get("normalization_reason")
        or llm_json.get("normalization_reason")
        or llm_result.get("normalization_reason")
        or (
            "insufficient_evidence_normalized_to_unknown"
            if row.get("assessment_status") == "insufficient_evidence"
            else None
        )
        or normalized.get("normalization_reason")
    )
    legacy_state_code = (
        fusion_json.get("legacy_state_code")
        or fusion_json.get("rule_legacy_state_code")
        or fusion_json.get("llm_legacy_state_code")
        or rule_json.get("legacy_state_code")
        or llm_json.get("legacy_state_code")
        or llm_result.get("legacy_state_code")
        or normalized.get("legacy_state_code")
    )
    return {
        "coarse_state_code": normalized["state_code"],
        "coarse_state_label": normalized["state_label"],
        "stage1_state_code": normalized["state_code"],
        "stage1_state_label": normalized["state_label"],
        "risk_level": normalized["risk_level"],
        "risk_label": normalized["risk_label"],
        "legacy_state_code": legacy_state_code or normalized["state_code"],
        "normalization_reason": normalization_reason,
        "evidence_tags": normalized["evidence_tags"],
        "candidate_scores": candidate_scores,
        "rule_state_code": normalize_state_payload(row.get("rule_state_code") or fusion_json.get("rule_state_code"))["state_code"],
        "llm_state_code": normalize_state_payload(row.get("llm_state_code") or fusion_json.get("llm_state_code"))["state_code"],
        "decision_source": fusion_json.get("decision_source"),
    }


def _decorate_detector_output(d):
    audit_state = _normalize_audit_state(d)
    raw_state_code = d.get("state_code")
    raw_fused_state_code = d.get("fused_state_code")
    d["raw_state_code"] = raw_state_code
    d["raw_fused_state_code"] = raw_fused_state_code
    d["state_code"] = audit_state["coarse_state_code"]
    d["fused_state_code"] = audit_state["coarse_state_code"]
    d["fused_state_label"] = audit_state["coarse_state_label"]
    d["coarse_state_code"] = audit_state["coarse_state_code"]
    d["coarse_state_label"] = audit_state["coarse_state_label"]
    d["stage1_state_code"] = audit_state["stage1_state_code"]
    d["stage1_state_label"] = audit_state["stage1_state_label"]
    d["state_semantics"] = "coarse_stage1"
    d.pop("final_state_code", None)
    d.pop("final_state_label", None)
    d["risk_level"] = audit_state["risk_level"]
    d["risk_label"] = audit_state["risk_label"]
    d["legacy_state_code"] = audit_state["legacy_state_code"]
    d["normalization_reason"] = audit_state["normalization_reason"]
    d["evidence_tags"] = audit_state["evidence_tags"]
    d["candidate_scores"] = audit_state["candidate_scores"]
    d["audit_state"] = audit_state
    return d


def _decorate_gate_record(d):
    cooldown_text = " ".join(
        str(d.get(key) or "")
        for key in ("decision_reason", "suppressed_reason", "reason")
    ).lower()
    cooldown_check = None
    if "cooldown" in cooldown_text:
        cooldown_check = {
            "ok": False,
            "cooling": True,
            "reason": d.get("suppressed_reason") or d.get("decision_reason"),
        }
    d["should_intervene"] = _boolish(d.get("should_intervene"))
    d["gating_result"] = {
        "should_intervene": d["should_intervene"],
        "decision": "intervene" if d["should_intervene"] else "skip",
        "reason": d.get("decision_reason") or d.get("suppressed_reason"),
        "suppressed_reason": d.get("suppressed_reason"),
        "selected_strategy_id": d.get("selected_strategy_id"),
        "target": d.get("target"),
        "priority": d.get("priority"),
        "strategy_category": d.get("strategy_category"),
        "cooldown_check": cooldown_check,
    }
    d["cooldown_check"] = cooldown_check
    return d


def _run_trace(run):
    if not run:
        return None
    metadata = _safe_json(run.get("metadata_json")) or {}
    validation = metadata.get("validation") or {}
    cooldown_check = validation.get("cooldown_check")
    triggerable_state_check = validation.get("triggerable_state_check")
    final_state = normalize_final_sub_state(
        run.get("canonical_sub_state_code")
        or metadata.get("final_sub_state_code")
        or metadata.get("canonical_sub_state_code")
    )
    coarse_state = normalize_state_payload(run.get("detected_state"))[
        "state_code"
    ]
    return {
        "run_id": run.get("id"),
        "status": run.get("status"),
        "final_sub_state_code": final_state,
        "final_sub_state_label": (
            FINAL_SUB_STATE_LABELS.get(final_state) if final_state else None
        ),
        "coarse_state_code": coarse_state,
        "trigger_source": run.get("trigger_type") or metadata.get("trigger_source"),
        "validated": run.get("validated"),
        "validation_error": run.get("validation_error"),
        "gating_result": {
            "valid": validation.get("valid"),
            "action": validation.get("action"),
            "reason": validation.get("reason") or run.get("failure_reason"),
            "triggerable_state_check": triggerable_state_check,
            "cooldown_check": cooldown_check,
            "active_run_check": validation.get("active_run_check"),
            "help_request_check": validation.get("help_request_check"),
            "cutoff_check": validation.get("cutoff_check"),
        },
        "cooldown_check": cooldown_check,
        "candidate_strategies": _safe_json(run.get("candidate_strategies")) or run.get("candidate_strategies"),
        "selected_strategy": _safe_json(run.get("selected_strategy")) or run.get("selected_strategy"),
        "context_from_sequence": run.get("context_from_sequence"),
        "context_to_sequence": run.get("context_to_sequence"),
        "input_message_sequences": _int_list(run.get("input_message_sequences_json")),
        "evidence_sequences": _int_list(run.get("evidence_sequences_json")),
        "selected_strategy_id": run.get("selected_strategy_id"),
        "generated_message": run.get("generated_message"),
        "prompt_version": run.get("prompt_version"),
        "review_error": run.get("validation_error") or run.get("failure_reason"),
        "actual_started_at": run.get("actual_started_at"),
        "actual_published_at": run.get("actual_published_at") or run.get("published_at"),
    }


def _state_system():
    return [
        {
            "code": code,
            "label": FINAL_SUB_STATE_LABELS[code],
            "is_primary": True,
            "is_process": False,
            "is_legacy": False,
        }
        for code in FINAL_SUB_STATE_CODES
    ] + [
        {
            "code": code,
            "label": PROCESS_STATE_LABELS[code],
            "is_primary": False,
            "is_process": True,
            "is_legacy": False,
        }
        for code in ("observing", "unclassified")
    ]


def _coarse_state_system():
    return [
        {
            "code": code,
            "label": LEGACY_STATE_LABELS[code],
            "is_primary": False,
            "is_process": False,
            "is_legacy": True,
            "debug_only": True,
        }
        for code in LEGACY_STATE_CODES
    ]


def _agent_kind_and_label(record):
    agent_type = str(record.get("agent_type") or "").strip().lower()
    source = str(record.get("trigger_source") or record.get("trigger_type") or "").strip().lower()
    mode = str(record.get("push_mode") or "").strip().lower()
    pushed_by = record.get("pushed_by_user_id")
    help_request_id = record.get("help_request_id")

    auto_sources = {
        "auto_state",
        "auto_v2",
        "auto_intervention",
        "new_message",
        "student_message",
        "sera_auto",
        "sera_auto_v2",
    }
    teacher_sources = {"teacher", "teacher_confirmed", "sera_teacher_confirmed"}

    if agent_type == "emotion":
        return "emotion", "情绪智能体"
    if help_request_id or "student_help" in source or mode == "student_request":
        return "strategy_student_help", "策略智能体 · 学生求助"
    if source in auto_sources or mode in ("sera_auto", "sera_auto_v2", "auto"):
        return "strategy_auto", "策略智能体 · 自动介入"
    if source in teacher_sources or "teacher" in source or mode in ("sera_teacher_confirmed", "teacher") or pushed_by:
        return "strategy_teacher", "策略智能体 · 教师介入"
    if source == "emotion_schedule":
        return "emotion", "情绪智能体"
    if (
        source == "legacy_unknown"
        or agent_type == "strategy"
        or record.get("strategy_id")
        or record.get("intervention_id")
        or record.get("intervention_run_id")
        or record.get("linked_log_id")
    ):
        return "legacy_agent", "Agent · legacy/未知来源"
    return "legacy_agent", "Agent · legacy/未知来源"


def _message_display_name(row, role):
    if role == "agent":
        return "SERA"
    return (
        row.get("participant_code")
        or row.get("real_name")
        or row.get("username")
        or ("用户 %s" % row.get("user_id"))
    )


def _message_role(row):
    role = str(row.get("role") or row.get("sender_type") or row.get("user_role") or "").strip().lower()
    if role in ("agent", "teacher"):
        return role
    return "student"


def _fetch_message_timeline(group_id, session_id, traceability):
    rows = query_all(
        """
        SELECT m.id, m.group_id, m.user_id, m.content, m.sequence,
               m.sender_type, m.role, m.session_no, m.task_id, m.session_id,
               m.strategy_id, m.linked_log_id, m.intervention_run_id,
               m.agent_type, m.agent_event_id, m.trigger_source AS message_trigger_source,
               m.created_at,
               u.real_name, u.username, u.participant_code, u.role AS user_role,
               il.trigger_source AS log_trigger_source,
               il.push_mode AS log_push_mode,
               il.pushed_by_user_id AS log_pushed_by_user_id,
               il.strategy_id AS log_strategy_id,
               il.help_request_id AS log_help_request_id,
               il.agent_type AS log_agent_type,
               ir.agent_type AS run_agent_type,
               ir.trigger_type AS run_trigger_type,
               ir.help_request_id AS run_help_request_id
          FROM messages m
          LEFT JOIN users u ON u.id = m.user_id
          LEFT JOIN intervention_logs il ON il.id = m.linked_log_id
          LEFT JOIN intervention_runs ir ON ir.id = m.intervention_run_id
         WHERE m.group_id=? AND m.session_id=?
         ORDER BY COALESCE(m.sequence, m.id), m.id
        """,
        (group_id, session_id),
    )
    messages = []
    for row in rows:
        d = dict(row)
        role = _message_role(d)
        agent_record = {
            "agent_type": d.get("agent_type") or d.get("log_agent_type") or d.get("run_agent_type"),
            "trigger_source": d.get("message_trigger_source") or d.get("log_trigger_source") or d.get("run_trigger_type"),
            "push_mode": d.get("log_push_mode"),
            "pushed_by_user_id": d.get("log_pushed_by_user_id"),
            "strategy_id": d.get("strategy_id") or d.get("log_strategy_id"),
            "intervention_run_id": d.get("intervention_run_id"),
            "linked_log_id": d.get("linked_log_id"),
            "help_request_id": d.get("log_help_request_id") or d.get("run_help_request_id"),
        }
        kind, label = _agent_kind_and_label(agent_record) if role == "agent" else (None, None)
        messages.append({
            "id": d.get("id"),
            "sequence": d.get("sequence") or d.get("id"),
            "created_at": d.get("created_at"),
            "role": role,
            "user_id": d.get("user_id"),
            "display_name": _message_display_name(d, role),
            "participant_code": d.get("participant_code"),
            "content": d.get("content") or "",
            "session_id": d.get("session_id"),
            "session_no": d.get("session_no"),
            "task_id": d.get("task_id"),
            "strategy_id": d.get("strategy_id") or d.get("log_strategy_id"),
            "linked_log_id": d.get("linked_log_id"),
            "intervention_run_id": d.get("intervention_run_id"),
            "agent_type": agent_record["agent_type"],
            "agent_trigger_source": agent_record["trigger_source"],
            "help_request_id": agent_record["help_request_id"],
            "agent_message_kind": kind,
            "agent_display_label": label,
        })
    if not messages:
        traceability.append(
            "[messages] No messages for group %s / session %s" % (group_id, session_id)
        )
    return messages


def _sequence_set(messages):
    result = set()
    for message in messages or []:
        if message.get("sequence") is not None:
            try:
                result.add(int(message["sequence"]))
            except (TypeError, ValueError):
                pass
    return result


def _review_belongs_to_session(row, session_sequences):
    cutoff = row.get("cutoff_sequence")
    try:
        if cutoff is not None and int(cutoff) in session_sequences:
            return True
    except (TypeError, ValueError):
        pass
    for seq in _int_list(row.get("input_message_sequences_json")):
        if seq in session_sequences:
            return True
    for seq in _int_list(row.get("evidence_sequences_json")):
        if seq in session_sequences:
            return True
    return False


def _rule_candidate_state(row):
    payload = _safe_json(row.get("rule_result_json")) or {}
    if isinstance(payload, dict):
        for key in ("state_code", "rule_state_code", "final_state", "primary_state"):
            if payload.get(key):
                return payload.get(key)
        candidates = payload.get("candidates")
        if isinstance(candidates, list) and candidates:
            first = candidates[0]
            if isinstance(first, dict):
                return first.get("state_code") or first.get("code") or first.get("state")
    return row.get("final_state") or row.get("detected_state")


def _review_failure(row, linked_run=None):
    return _first_value(
        row.get("review_error"),
        row.get("failure_reason") if row.get("status") in ("failed", "stale") else None,
        (linked_run or {}).get("validation_error"),
        (linked_run or {}).get("failure_reason") if (linked_run or {}).get("status") in ("FAILED", "STALE") else None,
        (linked_run or {}).get("skip_reason") if (linked_run or {}).get("status") == "CANCELLED" else None,
    )


def _fetch_strategy_reviews(group_id, session_id, message_timeline, traceability):
    session_sequences = _sequence_set(message_timeline)
    rows = query_all(
        """
        SELECT mr.*
          FROM monitor_runs mr
         WHERE mr.group_id=? AND mr.session_id=?
           AND (
                mr.review_decision IS NOT NULL
             OR mr.review_started_at IS NOT NULL
             OR mr.review_error IS NOT NULL
             OR mr.context_from_sequence IS NOT NULL
             OR mr.evidence_sequences_json IS NOT NULL
           )
         ORDER BY COALESCE(mr.review_started_at, mr.created_at), mr.id
        """,
        (group_id, session_id),
    )
    run_rows = query_all(
        """
        SELECT *
          FROM intervention_runs
         WHERE group_id=? AND session_id=? AND monitor_run_id IS NOT NULL
         ORDER BY id ASC
        """,
        (group_id, session_id),
    )
    runs_by_monitor = {}
    for run in run_rows:
        d = dict(run)
        runs_by_monitor.setdefault(d.get("monitor_run_id"), d)

    messages_by_sequence = {
        int(m["sequence"]): m
        for m in message_timeline
        if m.get("sequence") is not None
    }
    result = []
    for row in rows:
        d = dict(row)
        linked_run = runs_by_monitor.get(d.get("id"))
        if session_id and session_sequences and not _review_belongs_to_session(d, session_sequences):
            continue
        evidence_sequences = _int_list(d.get("evidence_sequences_json"))
        input_sequences = _int_list(d.get("input_message_sequences_json"))
        evidence_messages = []
        for seq in evidence_sequences:
            message = messages_by_sequence.get(seq)
            evidence_messages.append({
                "sequence": seq,
                "available": bool(message),
                "message_id": message.get("id") if message else None,
                "role": message.get("role") if message else None,
                "display_name": message.get("display_name") if message else None,
                "content": message.get("content") if message else None,
            })
        decision = d.get("review_decision")
        if not decision and linked_run and linked_run.get("status") == "CANCELLED":
            decision = "PASS"
        failure = _review_failure(d, linked_run)
        status = "failed" if failure and not decision else (decision or d.get("status") or "reviewed")
        result.append({
            "id": d.get("id"),
            "monitor_run_id": d.get("id"),
            "intervention_run_id": linked_run.get("id") if linked_run else None,
            "status": d.get("status"),
            "review_status": status,
            "trigger_type": d.get("trigger_type"),
            "rule_candidate_state": _rule_candidate_state(d),
            "rule_score": d.get("confidence"),
            "llm_decision": decision,
            "review_decision": decision,
            "llm_final_state": d.get("review_final_state"),
            "review_final_state": d.get("review_final_state"),
            "confidence": d.get("review_confidence"),
            "reason": d.get("review_reason"),
            "context_from_sequence": d.get("context_from_sequence"),
            "context_to_sequence": d.get("context_to_sequence") or d.get("cutoff_sequence"),
            "cutoff_sequence": d.get("cutoff_sequence"),
            "input_message_sequences": input_sequences,
            "evidence_sequences": evidence_sequences,
            "evidence_messages": evidence_messages,
            "strategy_id": d.get("selected_strategy_id"),
            "generated_message": d.get("generated_message"),
            "prompt_version": d.get("prompt_version"),
            "detected_at": d.get("created_at"),
            "review_started_at": d.get("review_started_at"),
            "review_completed_at": d.get("review_completed_at"),
            "published_at": linked_run.get("actual_published_at") if linked_run else None,
            "failure_reason": failure,
            "skip_reason": linked_run.get("skip_reason") if linked_run else None,
            "agent_type": "strategy",
            "agent_message_kind": "strategy_auto",
            "agent_display_label": "策略智能体 · 自动介入",
        })
    if rows and not result:
        traceability.append(
            "[strategy_reviews] Strategy review records exist for group %s but none match session %s"
            % (group_id, session_id)
        )
    return result


def _pipeline_history_value(value):
    return value if value not in (None, "") else HISTORY_NOT_RECORDED


def _pipeline_bool_text(value):
    if value is None:
        return HISTORY_NOT_RECORDED
    return "是" if bool(value) else "否"


def _pipeline_evidence_messages(refs, message_timeline):
    messages_by_sequence = {}
    messages_by_id = {}
    for message in message_timeline or []:
        if message.get("sequence") is not None:
            try:
                messages_by_sequence[int(message["sequence"])] = message
            except (TypeError, ValueError):
                pass
        if message.get("id") is not None:
            try:
                messages_by_id[int(message["id"])] = message
            except (TypeError, ValueError):
                pass

    result = []
    for ref in refs or []:
        try:
            number = int(ref)
        except (TypeError, ValueError):
            continue
        message = messages_by_sequence.get(number) or messages_by_id.get(number)
        matched_by = "sequence" if messages_by_sequence.get(number) else ("message_id" if message else None)
        result.append({
            "ref": number,
            "available": bool(message),
            "matched_by": matched_by,
            "sequence": message.get("sequence") if message else number,
            "message_id": message.get("id") if message else None,
            "role": message.get("role") if message else None,
            "display_name": message.get("display_name") if message else None,
            "content": message.get("content") if message else None,
        })
    return result


def _pipeline_observation_summary(row):
    details = _safe_json(row.get("observation_details_json")) or {}
    status = row.get("observation_status")
    result = row.get("observation_result")
    if not status and not result and not details:
        return HISTORY_NOT_RECORDED
    parts = []
    if status:
        parts.append("status=%s" % status)
    if result:
        parts.append("result=%s" % result)
    if row.get("observation_previous_sub_state_code") or row.get("observation_current_sub_state_code"):
        parts.append(
            "%s→%s" % (
                row.get("observation_previous_sub_state_code") or "?",
                row.get("observation_current_sub_state_code") or "?",
            )
        )
    if row.get("observation_first_response_sequence") is not None:
        parts.append("first_response=#%s" % row.get("observation_first_response_sequence"))
    return "；".join(parts) if parts else _json_dumps_for_audit(details)


def _json_dumps_for_audit(value):
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _fetch_strategy_pipeline_runs(group_id, session_id, message_timeline, blinded, traceability):
    if not _table_exists("strategy_pipeline_runs"):
        traceability.append(
            "[strategy_pipeline_runs] 三阶段审计表不存在；历史数据未记录"
        )
        return []

    rows = query_all(
        """
        SELECT spr.*,
               g.group_code,
               g.condition AS group_condition,
               COALESCE(es.session_no, spr.session_no, '') AS export_session_no,
               pm.content AS published_message_content
          FROM strategy_pipeline_runs spr
          LEFT JOIN groups g ON g.id = spr.group_id
          LEFT JOIN experiment_sessions es ON es.id = spr.session_id
          LEFT JOIN messages pm ON pm.id = spr.published_message_id
         WHERE spr.group_id=? AND spr.session_id=?
         ORDER BY COALESCE(spr.stage1_started_at, spr.created_at), spr.id
        """,
        (group_id, session_id),
    )
    rows_by_id = {str(row["id"]): dict(row) for row in rows}
    result = []
    for row in rows:
        d = dict(row)
        replacement_row = rows_by_id.get(
            str(d.get("replaced_by_pipeline_run_id"))
        )
        replacement_stage2_succeeded = bool(
            replacement_row
            and str(replacement_row.get("stage2_status") or "").upper()
            == "SUCCEEDED"
        )
        coarse_evidence = _int_list(d.get("coarse_evidence_message_ids_json"))
        sub_state_evidence = _int_list(d.get("sub_state_evidence_message_ids_json"))
        candidate_ids = _safe_json(d.get("strategy_candidate_ids_json")) or []
        supporting_ids = _safe_json(d.get("supporting_strategy_ids_json")) or []
        secondary_tags = _safe_json(d.get("secondary_sub_state_tags_json")) or []
        rule_scores = _safe_json(d.get("coarse_rule_scores_json")) or {}
        quantitative = _safe_json(d.get("coarse_quantitative_features_json")) or {}
        validation = _safe_json(d.get("text_validation_result_json")) or {}
        observation_details = _safe_json(d.get("observation_details_json")) or {}
        final_sub_state = normalize_final_sub_state(
            d.get("canonical_sub_state_code")
        )
        generated_text = d.get("generated_intervention_text") or ""
        validated_text = d.get("validated_intervention_text") or ""
        if blinded:
            generated_text = "[BLINDED AGENT MESSAGE]" if generated_text else ""
            validated_text = "[BLINDED AGENT MESSAGE]" if validated_text else ""
            d["published_message_content"] = "[BLINDED AGENT MESSAGE]" if d.get("published_message_content") else ""
            d.pop("group_condition", None)

        item = {
            "id": d.get("id"),
            "pipeline_run_id": d.get("id"),
            "run_uuid": d.get("run_uuid"),
            "group_id": d.get("group_id"),
            "group_code": d.get("group_code"),
            "group_condition": d.get("group_condition"),
            "session_id": d.get("session_id"),
            "session_no": d.get("export_session_no") or d.get("session_no"),
            "discussion_id": d.get("discussion_id"),
            "task_id": d.get("task_id"),
            "trigger_source": _pipeline_history_value(d.get("trigger_source")),
            "trigger_message_id": d.get("trigger_message_id"),
            "trigger_priority": d.get("trigger_priority"),
            "trigger_level_state": _pipeline_history_value(d.get("trigger_level_state")),
            "latest_state": _pipeline_history_value(d.get("latest_state")),
            "latest_should_intervene": _pipeline_bool_text(
                d.get("latest_should_intervene")
            ),
            "latest_state_pipeline_run_id": d.get("latest_state_pipeline_run_id"),
            "parent_run_id": d.get("parent_run_id"),
            "superseded_by_run_id": d.get("superseded_by_run_id"),
            "replaced_by_pipeline_run_id": d.get("replaced_by_pipeline_run_id"),
            "replacement_reason": d.get("replacement_reason") or HISTORY_NOT_RECORDED,
            "replacement_trigger_message_id": d.get("replacement_trigger_message_id"),
            "replacement_cutoff_sequence": d.get("replacement_cutoff_sequence"),
            "original_pipeline_run_id": d.get("id"),
            "original_pipeline_terminal": _pipeline_history_value(d.get("final_status")),
            "replacement_pipeline_run_id": (
                replacement_row.get("id") if replacement_row else None
            ),
            "replacement_final_state": (
                _pipeline_history_value(
                    replacement_row.get("canonical_sub_state_code")
                )
                if replacement_stage2_succeeded
                else HISTORY_NOT_RECORDED
            ),
            "replacement_should_intervene": _pipeline_bool_text(
                replacement_row.get("should_intervene")
                if replacement_stage2_succeeded
                else None
            ),
            "replacement_publish_status": (
                replacement_row.get("publish_status")
                if replacement_row and replacement_row.get("publish_status")
                else HISTORY_NOT_RECORDED
            ),
            "input_start_sequence": d.get("input_start_sequence"),
            "input_end_sequence": d.get("input_end_sequence"),
            "input_cutoff_student_sequence": d.get("input_cutoff_student_sequence"),
            "stage1_status": _pipeline_history_value(d.get("stage1_status")),
            "coarse_decision": _pipeline_history_value(d.get("coarse_decision")),
            "coarse_state_code": _pipeline_history_value(d.get("coarse_state_code")),
            "coarse_risk_group": _pipeline_history_value(d.get("coarse_risk_group")),
            "coarse_should_escalate": _pipeline_bool_text(d.get("coarse_should_escalate")),
            "coarse_confidence": d.get("coarse_confidence"),
            "coarse_rule_scores": rule_scores,
            "coarse_quantitative_features": quantitative,
            "coarse_evidence_message_ids": coarse_evidence,
            "coarse_reason_codes": _safe_json(d.get("coarse_reason_codes_json")) or [],
            "stage2_status": _pipeline_history_value(d.get("stage2_status")),
            "raw_sub_state_code": _pipeline_history_value(d.get("raw_sub_state_code")),
            "canonical_sub_state_code": _pipeline_history_value(d.get("canonical_sub_state_code")),
            "final_sub_state_code": final_sub_state,
            "final_sub_state_label": (
                FINAL_SUB_STATE_LABELS.get(final_sub_state)
                if final_sub_state
                else None
            ),
            "assessment_status": (
                "confirmed" if final_sub_state else "unclassified"
            ),
            "assignment_source": "strategy_pipeline",
            "secondary_sub_state_tags": secondary_tags,
            "sub_state_confidence": d.get("sub_state_confidence"),
            "sub_state_reason": _pipeline_history_value(d.get("sub_state_reason")),
            "sub_state_start_sequence": d.get("sub_state_start_sequence"),
            "sub_state_end_sequence": d.get("sub_state_end_sequence"),
            "evidence_message_ids": sub_state_evidence,
            "evidence_sequences": sub_state_evidence,
            "evidence_messages": _pipeline_evidence_messages(sub_state_evidence, message_timeline),
            "all_state_segments": _safe_json(d.get("all_state_segments_json")) or [],
            "detected_self_regulation": _pipeline_bool_text(d.get("detected_self_regulation")),
            "should_intervene": _pipeline_bool_text(d.get("should_intervene")),
            "inhibition_strategy_id": d.get("inhibition_strategy_id") or HISTORY_NOT_RECORDED,
            "inhibition_reason": d.get("inhibition_reason") or HISTORY_NOT_RECORDED,
            "stage3_status": _pipeline_history_value(d.get("stage3_status")),
            "strategy_candidate_ids": candidate_ids,
            "selected_strategy_id": d.get("selected_strategy_id") or HISTORY_NOT_RECORDED,
            "strategy_id": d.get("selected_strategy_id") or "",
            "selected_strategy_name": d.get("selected_strategy_name") or HISTORY_NOT_RECORDED,
            "selected_strategy_type": d.get("selected_strategy_type") or HISTORY_NOT_RECORDED,
            "supporting_strategy_ids": supporting_ids,
            "strategy_selection_reason": d.get("strategy_selection_reason") or HISTORY_NOT_RECORDED,
            "strategy_library_version": d.get("strategy_library_version") or HISTORY_NOT_RECORDED,
            "generated_intervention_text": generated_text or HISTORY_NOT_RECORDED,
            "validated_intervention_text": validated_text or HISTORY_NOT_RECORDED,
            "text_validation_result": validation or HISTORY_NOT_RECORDED,
            "publish_status": d.get("publish_status") or HISTORY_NOT_RECORDED,
            "published_message_id": d.get("published_message_id"),
            "published_at": d.get("published_at"),
            "published_message_content": d.get("published_message_content") or "",
            "final_status": d.get("final_status") or HISTORY_NOT_RECORDED,
            "skip_reason": d.get("skip_reason") or HISTORY_NOT_RECORDED,
            "failure_code": d.get("failure_code") or HISTORY_NOT_RECORDED,
            "failure_detail": d.get("failure_detail") or HISTORY_NOT_RECORDED,
            "observation_status": d.get("observation_status") or HISTORY_NOT_RECORDED,
            "observation_result": d.get("observation_result") or HISTORY_NOT_RECORDED,
            "observation_summary": _pipeline_observation_summary(d),
            "observation_details": observation_details,
            "observation_previous_sub_state_code": d.get("observation_previous_sub_state_code") or HISTORY_NOT_RECORDED,
            "observation_current_sub_state_code": d.get("observation_current_sub_state_code") or HISTORY_NOT_RECORDED,
            "observation_first_response_sequence": d.get("observation_first_response_sequence"),
            "observation_window_start_sequence": d.get("observation_window_start_sequence"),
            "observation_window_end_sequence": d.get("observation_window_end_sequence"),
            "created_at": d.get("created_at"),
            "updated_at": d.get("updated_at"),
            "agent_message_kind": "strategy_auto",
            "agent_display_label": "策略智能体 · 三阶段",
            "legacy_state_code": d.get("coarse_state_code") or "",
        }
        result.append(item)

    if not rows:
        traceability.append(
            "[strategy_pipeline_runs] No three-stage pipeline records for group %s / session %s; 历史数据未记录"
            % (group_id, session_id)
        )
    return result


def _fetch_assessment_batches(group_id, session_id, traceability):
    if not _table_exists("state_assessment_batches"):
        traceability.append(
            "[state_assessment_batches] 状态批次表不存在；历史数据未记录"
        )
        return []
    rows = query_all(
        """
        SELECT id, group_id, session_id, session_no, task_id, discussion_id,
               candidate_start_sequence, candidate_end_sequence,
               context_start_sequence, context_end_sequence,
               trigger_type, trigger_sequence, status, terminal_status,
               attempt_count, max_attempts, model, prompt_version,
               error_code, error_detail, fallback_action,
               fallback_segment_count, raw_response,
               started_at, completed_at, terminal_at, created_at, updated_at
        FROM state_assessment_batches
        WHERE group_id=? AND session_id=?
        ORDER BY COALESCE(completed_at, terminal_at, created_at), id
        """,
        (group_id, session_id),
    )
    result = []
    for row in rows:
        item = dict(row)
        raw_response = item.pop("raw_response", None)
        item["batch_id"] = item.get("id")
        item["raw_response_length"] = len(raw_response or "")
        item["llm_error"] = {
            "error_code": item.get("error_code"),
            "error_detail": item.get("error_detail"),
        }
        item["assessment_status"] = (
            "unclassified"
            if item.get("terminal_status") in {"degraded", "quarantined"}
            or item.get("status") in {"failed", "superseded"}
            else "confirmed"
            if item.get("status") == "succeeded"
            else "observing"
        )
        item["assignment_source"] = (
            "batch_unclassified"
            if item["assessment_status"] == "unclassified"
            else "assessment_batch"
        )
        result.append(item)
    return result


def _audit_stats(message_timeline, interventions, strategy_reviews, strategy_pipeline_runs=None):
    agent_messages = [m for m in message_timeline if m.get("role") == "agent"]
    def count_kind(items, kind):
        return len([item for item in items if item.get("agent_message_kind") == kind])

    visible_interventions = [
        item for item in interventions
        if item.get("student_visible_message") is not False
    ]

    pass_reviews = [
        r for r in strategy_reviews
        if str(r.get("llm_decision") or "").upper() == "PASS"
    ]
    failed_reviews = [
        r for r in strategy_reviews
        if r.get("failure_reason") or str(r.get("review_status") or "").lower() == "failed"
    ]
    published_strategy_reviews = [
        r for r in strategy_reviews
        if str(r.get("llm_decision") or "").upper() == "INTERVENE" and not r.get("failure_reason")
    ]
    auto_count = (
        len([p for p in (strategy_pipeline_runs or []) if str(p.get("publish_status") or "").upper() == "PUBLISHED"])
        or len(published_strategy_reviews)
        or count_kind(visible_interventions, "strategy_auto")
        or count_kind(agent_messages, "strategy_auto")
    )
    help_count = count_kind(visible_interventions, "strategy_student_help") or count_kind(agent_messages, "strategy_student_help")
    teacher_count = count_kind(visible_interventions, "strategy_teacher") or count_kind(agent_messages, "strategy_teacher")
    return {
        "agent_message_total": len(agent_messages),
        "emotion_agent_message_count": count_kind(agent_messages, "emotion"),
        "strategy_auto_intervention_count": auto_count,
        "student_help_reply_count": help_count,
        "teacher_strategy_intervention_count": teacher_count,
        "llm_pass_review_count": len(pass_reviews),
        "llm_failed_review_count": len(failed_reviews),
        "three_stage_pipeline_count": len(strategy_pipeline_runs or []),
        "three_stage_published_count": len([
            p for p in (strategy_pipeline_runs or [])
            if str(p.get("publish_status") or "").upper() == "PUBLISHED"
        ]),
        "actual_intervention_count": auto_count + help_count + teacher_count,
        "scope_note": "Agent 消息总数按当前课次 role=agent 消息计数；实际介入数不包含 PASS 或失败复核。",
    }


def get_agent_audit(group_id, session_id, blinded=True):
    """Return the full T5 audit chain for a group/session.

    Returns dict with keys:
      detector_outputs, gate_records, interventions, uptake,
      autonomous_regulation_events, traceability_warnings
    """
    traceability = []
    message_timeline = _fetch_message_timeline(group_id, session_id, traceability)

    # ---- 1. Detector outputs (state_assessments) ----
    detector_outputs = _fetch_detector_outputs(group_id, session_id, blinded, traceability)

    # ---- 2. Gate records (intervention_decisions) ----
    gate_records = _fetch_gate_records(group_id, session_id, blinded, traceability)

    # ---- 3. Interventions (intervention_logs + intervention_runs) ----
    interventions = _fetch_interventions(group_id, session_id, blinded, traceability)

    # ---- 4. Uptake (intervention_uptake) ----
    uptake = _fetch_uptake(group_id, session_id, blinded, traceability)

    # ---- 5. Autonomous regulation events ----
    auto_reg_events = _fetch_autonomous_regulation_events(group_id, session_id, traceability)

    # ---- 6. Unified strategy review records (monitor_runs) ----
    strategy_reviews = _fetch_strategy_reviews(group_id, session_id, message_timeline, traceability)

    # ---- 7. Three-stage strategy pipeline audit ----
    strategy_pipeline_runs = _fetch_strategy_pipeline_runs(
        group_id,
        session_id,
        message_timeline,
        blinded,
        traceability,
    )
    assessment_batches = _fetch_assessment_batches(
        group_id,
        session_id,
        traceability,
    )
    stats = _audit_stats(message_timeline, interventions, strategy_reviews, strategy_pipeline_runs)

    return {
        "state_system": _state_system(),
        "coarse_state_system": _coarse_state_system(),
        "coarse_state_debug_only": True,
        "stats": stats,
        "message_timeline": message_timeline,
        "strategy_reviews": strategy_reviews,
        "strategy_pipeline_runs": strategy_pipeline_runs,
        "assessment_batches": assessment_batches,
        "detector_outputs": detector_outputs,
        "gate_records": gate_records,
        "interventions": interventions,
        "uptake": uptake,
        "autonomous_regulation_events": auto_reg_events,
        "traceability_warnings": traceability,
    }


# ---------------------------------------------------------------------------
# Internal fetchers
# ---------------------------------------------------------------------------

def _fetch_detector_outputs(group_id, session_id, blinded, traceability):
    """Query state_assessments.  Missing optional columns -> null in output."""
    rows = query_all(
        """SELECT sa.*
             FROM state_assessments sa
            WHERE sa.group_id=? AND sa.session_id=?
            ORDER BY sa.id ASC""",
        (group_id, session_id),
    )
    outputs = []
    for r in rows:
        d = dict(r)
        # Normalise boolean-ish fields for JSON
        for key in ("should_intervene", "fallback_used", "self_regulation_detected"):
            if key in d and d[key] is not None:
                d[key] = bool(int(d[key]))
        if blinded:
            d.pop("condition", None)
        outputs.append(_decorate_detector_output(d))

    if not rows:
        traceability.append(
            "[state_assessments] No detector output records for group %s / session %s" % (group_id, session_id)
        )
    return outputs


def _fetch_gate_records(group_id, session_id, blinded, traceability):
    """Query intervention_decisions.  Missing fields return null."""
    rows = query_all(
        """SELECT id.*
             FROM intervention_decisions id
            WHERE id.group_id=? AND id.session_id=?
            ORDER BY id.id ASC""",
        (group_id, session_id),
    )
    # Try fallback: find by joining through state_assessments if no direct match
    if not rows:
        # Check if there are assessment-related decisions
        sa_ids = query_all(
            "SELECT id FROM state_assessments WHERE group_id=? AND session_id=?", (group_id, session_id)
        )
        if sa_ids:
            ids = tuple(r["id"] for r in sa_ids)
            placeholders = ",".join("?" for _ in ids)
            rows = query_all(
                "SELECT id.* FROM intervention_decisions id WHERE id.assessment_id IN (%s) ORDER BY id.id ASC" % placeholders,
                ids,
            )
    records = []
    for r in rows:
        d = dict(r)
        if blinded:
            d.pop("condition", None)
            d.pop("selected_strategy_id", None)
        records.append(_decorate_gate_record(d))

    # Traceability for missing gate fields
    if rows:
        sample = dict(rows[0])
        missing = [k for k in ("target", "priority", "strategy_category", "selected_strategy_id", "decision_reason")
                   if sample.get(k) is None]
        if missing:
            traceability.append(
                "[gate_records] intervention_decisions missing fields: %s.  Original auto results unchanged." % ", ".join(missing)
            )
    else:
        traceability.append(
            "[gate_records] No gate (intervention_decisions) records for group %s / session %s" % (group_id, session_id)
        )
    return records


def _fetch_interventions(group_id, session_id, blinded, traceability):
    """Query intervention_logs and intervention_runs."""
    logs = query_all(
        """SELECT il.*
             FROM intervention_logs il
            WHERE il.group_id=? AND il.session_id=?
            ORDER BY il.id ASC""",
        (group_id, session_id),
    )
    runs = query_all(
        """SELECT ir.*
             FROM intervention_runs ir
            WHERE ir.group_id=? AND ir.session_id=?
            ORDER BY ir.id ASC""",
        (group_id, session_id),
    )

    # Cross-reference
    result = []
    for lr in logs:
        d = dict(lr)
        if blinded:
            d.pop("condition", None)
            d.pop("template_id", None)
        # Try to link to run - V2 pipeline stores intervention_run_id in intervention_id
        linked_id = d.get("intervention_run_id") or d.get("intervention_id")
        if linked_id:
            matched = [dict(r) for r in runs if r["id"] == int(linked_id)]
            d["linked_run"] = matched[0] if matched else None
        else:
            d["linked_run"] = None
        if d.get("linked_run"):
            d["help_request_id"] = d.get("help_request_id") or d["linked_run"].get("help_request_id")
        d["intervention_trace"] = _run_trace(d.get("linked_run"))
        d["cooldown_check"] = (
            d["intervention_trace"].get("cooldown_check")
            if d.get("intervention_trace")
            else None
        )
        if d.get("linked_run"):
            trace = d.get("intervention_trace") or {}
            d["final_sub_state_code"] = trace.get("final_sub_state_code")
            d["final_sub_state_label"] = trace.get("final_sub_state_label")
            d["coarse_state_code"] = trace.get("coarse_state_code")
            d["trigger_source"] = d.get("trigger_source") or trace.get("trigger_source")
            d["agent_type"] = d.get("agent_type") or d["linked_run"].get("agent_type")
            d["context_from_sequence"] = trace.get("context_from_sequence")
            d["context_to_sequence"] = trace.get("context_to_sequence")
            d["input_message_sequences"] = trace.get("input_message_sequences")
            d["evidence_sequences"] = trace.get("evidence_sequences")
            d["selected_strategy_id"] = trace.get("selected_strategy_id")
            d["generated_message"] = trace.get("generated_message")
            d["review_error"] = trace.get("review_error")
            d["actual_started_at"] = trace.get("actual_started_at")
            d["actual_published_at"] = trace.get("actual_published_at")
        kind, label = _agent_kind_and_label(d)
        d["agent_message_kind"] = kind
        d["agent_display_label"] = label
        d["student_visible_message"] = True
        result.append(d)

    logged_run_ids = {
        item.get("intervention_run_id") or item.get("intervention_id")
        for item in result
        if item.get("intervention_run_id") or item.get("intervention_id")
    }
    for run in runs:
        r = dict(run)
        run_id = r.get("id")
        if run_id in logged_run_ids:
            continue
        if session_id and r.get("session_id") and int(r.get("session_id")) != int(session_id):
            continue
        status = str(r.get("status") or "").upper()
        if status not in ("SKIPPED", "PASS", "FAILED", "STALE", "DRY_RUN", "CANCELLED"):
            continue
        trace = _run_trace(r)
        d = {
            "id": None,
            "intervention_id": run_id,
            "intervention_run_id": run_id,
            "group_id": r.get("group_id"),
            "session_id": r.get("session_id"),
            "task_id": r.get("task_id"),
            "created_at": r.get("completed_at") or r.get("created_at"),
            "push_mode": None,
            "trigger_source": r.get("trigger_type") or (trace or {}).get("trigger_source") or "legacy_unknown",
            "agent_type": r.get("agent_type") or "strategy",
            "help_request_id": r.get("help_request_id"),
            "message": r.get("generated_message") or "",
            "title": r.get("selected_strategy_id") or r.get("strategy_id") or status,
            "strategy_id": r.get("selected_strategy_id") or r.get("strategy_id"),
            "linked_run": r,
            "intervention_trace": trace,
            "cooldown_check": (trace or {}).get("cooldown_check"),
            "final_sub_state_code": (trace or {}).get("final_sub_state_code"),
            "final_sub_state_label": (trace or {}).get(
                "final_sub_state_label"
            ),
            "coarse_state_code": (trace or {}).get("coarse_state_code"),
            "review_error": (trace or {}).get("review_error"),
            "actual_started_at": (trace or {}).get("actual_started_at"),
            "actual_published_at": (trace or {}).get("actual_published_at"),
            "student_visible_message": False,
        }
        kind, label = _agent_kind_and_label(d)
        d["agent_message_kind"] = kind
        d["agent_display_label"] = label
        result.append(d)

    result.sort(key=lambda item: (item.get("created_at") or "", item.get("intervention_run_id") or item.get("id") or 0))

    if not logs:
        traceability.append(
            "[interventions] No intervention_logs for group %s / session %s" % (group_id, session_id)
        )
    if not runs:
        traceability.append(
            "[interventions] No intervention_runs found for group %s" % group_id
        )
    return result


def _fetch_uptake(group_id, session_id, blinded, traceability):
    """Query intervention_uptake."""
    rows = query_all(
        """SELECT iu.*
             FROM intervention_uptake iu
            WHERE iu.group_id=? AND iu.session_id=?
            ORDER BY iu.id ASC""",
        (group_id, session_id),
    )
    result = [dict(r) for r in rows]
    if not rows:
        traceability.append(
            "[uptake] No intervention_uptake records for group %s / session %s" % (group_id, session_id)
        )
    return result


def _fetch_autonomous_regulation_events(group_id, session_id, traceability):
    """Query autonomous_regulation_events."""
    rows = query_all(
        """SELECT are.*
             FROM autonomous_regulation_events are
            WHERE are.group_id=? AND are.session_id=?
            ORDER BY are.id ASC""",
        (group_id, session_id),
    )
    result = [dict(r) for r in rows]
    if not rows:
        traceability.append(
            "[autonomous_regulation] No autonomous regulation events for group %s / session %s" % (group_id, session_id)
        )
    return result


# ---------------------------------------------------------------------------
# Manual uptake correction
# ---------------------------------------------------------------------------

def record_manual_uptake(intervention_log_id, manual_uptake_type, *, corrected_by, reason=None):
    """Record a manual uptake correction.

    - Writes to intervention_uptake (upsert by intervention_log_id).
    - Writes audit_logs.
    - Does NOT modify original auto detection results.
    - manual_uptake_type must be one of the valid six.
    """
    if manual_uptake_type not in VALID_UPTAKE_TYPES:
        raise ValueError(
            "Invalid manual_uptake_type '%s'. Must be one of: %s" %
            (manual_uptake_type, ", ".join(sorted(VALID_UPTAKE_TYPES)))
        )

    # Find the intervention_log to get group_id / session_id
    log_row = query_one(
        "SELECT id, group_id, session_id, intervention_id, title, message FROM intervention_logs WHERE id=?", (intervention_log_id,)
    )
    if not log_row:
        raise ValueError("intervention_log %s not found" % intervention_log_id)

    now = now_str()
    upt = query_one(
        "SELECT id FROM intervention_uptake WHERE intervention_id=? AND group_id=? AND session_id=? ORDER BY id DESC LIMIT 1",
        (log_row["intervention_id"], log_row["group_id"], log_row["session_id"]),
    )

    if upt:
        execute(
            """UPDATE intervention_uptake
                   SET manual_uptake_type=?,
                       corrected_by=?,
                       corrected_at=?,
                       reason=?
                 WHERE id=?""",
            (manual_uptake_type, corrected_by, now, reason, upt["id"]),
        )
    else:
        execute(
            """INSERT INTO intervention_uptake
                   (intervention_id, group_id, session_id, manual_uptake_type, corrected_by, corrected_at, reason)
                 VALUES (?,?,?,?,?,?,?)""",
            (log_row["intervention_id"], log_row["group_id"], log_row["session_id"],
             manual_uptake_type, corrected_by, now, reason),
        )

    write_audit_log(
        operator_id=corrected_by,
        action_type="intervention.manual_uptake",
        target_type="intervention_log",
        target_id=intervention_log_id,
        after_value=json.dumps({"manual_uptake_type": manual_uptake_type}, ensure_ascii=False),
        reason=reason or "Manual uptake correction to %s" % manual_uptake_type,
    )

    return {"ok": True, "manual_uptake_type": manual_uptake_type, "corrected_at": now}


# ---------------------------------------------------------------------------
# Unblind audit recording
# ---------------------------------------------------------------------------

def record_unblind(*, operator_id, reason):
    """Record that a teacher performed a non-blind view.

    Returns the audit_log entry.
    """
    write_audit_log(
        operator_id=operator_id,
        action_type="audit.unblind",
        target_type="agent_audit",
        target_id=None,
        before_value="blinded",
        after_value="unblinded",
        reason=reason or "Unblinded agent audit view",
    )
    return {"ok": True, "action": "unblinded", "reason": reason}


# ---------------------------------------------------------------------------
# Helper: list groups for selector
# ---------------------------------------------------------------------------

def list_groups_with_sessions():
    """Return list of groups with their available session IDs."""
    groups = query_all(
        "SELECT id, name, group_code FROM groups ORDER BY id ASC"
    )
    # Fetch all non-draft experiment sessions (global, not per-group).
    # Using direct lookup instead of JOIN through messages so that
    # sessions appear even when no messages have been sent yet.
    all_sessions = query_all(
        """SELECT id, session_no, session_role
             FROM experiment_sessions
            WHERE status != 'draft'
            ORDER BY session_no ASC"""
    )
    session_list = [dict(s) for s in all_sessions]
    result = []
    for g in groups:
        result.append({
            "group_id": g["id"],
            "name": g["name"] or g["group_code"] or ("Group %s" % g["id"]),
            "sessions": session_list,
        })
    return result
