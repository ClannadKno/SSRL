# -*- coding: utf-8 -*-
"""Phase 8 state assessment persistence, evidence fusion, and confirmation."""
import json

from config import STATE_CONFIRM_WINDOWS
from db import db, execute, now_str, query_all, query_one
from knowledge_base import STATE_META, normalize_state_payload

FUSION_VERSION = "phase8_state_fusion_v1"
CONFIRMATION_VERSION = "phase8_state_confirm_v1"
DETECTOR_VERSION = "phase23_analysis_audit_v1"


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_llm_payload(llm_result):
    if not isinstance(llm_result, dict):
        return {}
    if llm_result.get("detector_error"):
        return {}
    payload = dict(llm_result)
    state_code = payload.get("primary_state") or payload.get("state_code")
    if state_code:
        normalized = normalize_state_payload(
            state_code,
            evidence_tags=payload.get("evidence_tags") or payload.get("secondary_flags"),
            assessment_status=payload.get("assessment_status"),
        )
        payload["state_code"] = normalized["state_code"]
        payload["primary_state"] = normalized["state_code"]
        payload["legacy_state_code"] = normalized["legacy_state_code"]
        payload["normalization_reason"] = normalized["normalization_reason"]
        payload["evidence_tags"] = normalized["evidence_tags"]
    return payload


def _build_llm_assessment_payload(llm_result=None, llm_meta=None):
    payload = dict(llm_meta or {})
    normalized = _normalize_llm_payload(llm_result)
    if normalized:
        payload["result"] = normalized
        payload.setdefault("state_code", normalized.get("state_code"))
        payload.setdefault("confidence", normalized.get("confidence"))
    return payload or None


def _build_analysis_audit(rule_state, llm_result=None, llm_meta=None):
    llm_meta = dict(llm_meta or {})
    llm_payload = _normalize_llm_payload(llm_result)
    rule_assessment = dict((rule_state or {}).get("rule_assessment") or {})
    request_started_at = llm_meta.get("request_started_at") or (rule_state or {}).get("analysis_started_at") or now_str()
    request_finished_at = llm_meta.get("request_finished_at") or (rule_state or {}).get("analysis_finished_at") or request_started_at
    latency_ms = _safe_int(llm_meta.get("latency_ms"), None)
    if latency_ms is None:
        latency_ms = _safe_int((rule_state or {}).get("analysis_latency_ms"), None)
    llm_attempted = bool(llm_meta) and not llm_meta.get("analysis_skipped")
    fallback_used = False
    if not llm_payload and llm_attempted:
        fallback_used = True
    error_message = (
        llm_meta.get("error_message")
        or llm_meta.get("failure_message")
        or llm_meta.get("failure_reason")
        or llm_meta.get("failure_type")
    )
    return {
        "rule_version": rule_assessment.get("version"),
        "detector_version": (rule_state or {}).get("detector_version") or DETECTOR_VERSION,
        "model_name": llm_meta.get("model_name") or llm_payload.get("model_name"),
        "prompt_version": llm_meta.get("prompt_version") or llm_payload.get("prompt_version"),
        "request_started_at": request_started_at,
        "request_finished_at": request_finished_at,
        "latency_ms": latency_ms,
        "fallback_used": fallback_used,
        "error_message": error_message,
    }


def _coerce_confirmation_window_count():
    try:
        return max(1, int(STATE_CONFIRM_WINDOWS))
    except Exception:
        return 1


def _confirmation_scope_params(assessment_row):
    return (
        assessment_row["group_id"],
        assessment_row["session_id"],
        assessment_row["session_id"],
        assessment_row["discussion_id"],
        assessment_row["discussion_id"],
        assessment_row["session_no"],
        assessment_row["session_no"],
        assessment_row["task_id"],
        assessment_row["task_id"],
        assessment_row["id"],
    )


def _llm_failure_blocks_intervention(llm_meta):
    if not isinstance(llm_meta, dict) or not llm_meta:
        return False
    if llm_meta.get("analysis_skipped"):
        return False
    if llm_meta.get("llm_required") is False:
        return False
    validation_failed = llm_meta.get("validation_status") == "failed" or llm_meta.get("schema_valid") is False
    call_failed = llm_meta.get("analysis_failed") or llm_meta.get("success") is False
    fallback_required = llm_meta.get("fallback_required")
    return bool(validation_failed or call_failed or fallback_required)


def _llm_can_override_rule(rule_state_code, rule_score, llm_state_code, llm_confidence):
    if llm_confidence < 0.70:
        return False
    if rule_state_code == "unknown":
        return True
    review_states = {"conflict_tension", "negative_silence", "blocked_frustration", "task_detached"}
    if rule_state_code in review_states and llm_state_code in review_states:
        return llm_confidence >= max(0.72, rule_score - 0.05)
    return llm_confidence >= max(0.75, rule_score + 0.10)


def fuse_state_evidence(rule_state, llm_result=None, llm_meta=None):
    """Combine rule and optional LLM evidence into a single Phase 8 assessment."""
    rule_assessment = dict((rule_state or {}).get("rule_assessment") or {})
    raw_rule_state_code = (rule_state or {}).get("state_code") or "unknown"
    rule_norm = normalize_state_payload(
        raw_rule_state_code,
        evidence_tags=(rule_state or {}).get("evidence_tags"),
        assessment_status=rule_assessment.get("assessment_status"),
    )
    rule_state_code = rule_norm["state_code"]
    rule_state_label = rule_norm["state_label"]
    rule_risk_level = rule_norm["risk_level"]
    rule_risk_label = rule_norm["risk_label"]
    rule_score = round(_safe_float((rule_state or {}).get("state_score"), 0.0), 3)
    rule_status = rule_assessment.get("assessment_status") or "insufficient_evidence"

    llm_payload = _normalize_llm_payload(llm_result)
    llm_state_code = llm_payload.get("state_code")
    llm_confidence = round(_safe_float(llm_payload.get("confidence"), 0.0), 3)
    llm_status = llm_payload.get("assessment_status") or ("confirmed" if llm_state_code else None)
    llm_valid = bool(
        llm_state_code in STATE_META
        and not llm_payload.get("detector_error")
        and (
            (llm_meta or {}).get("success") is True
            or (llm_meta or {}).get("validation_status") == "passed"
            or not llm_meta
        )
    )
    self_regulation_detected = bool(
        llm_payload.get("self_regulation_detected")
        or rule_assessment.get("self_regulation_detected")
    )
    self_regulation_sub_type = (
        llm_payload.get("self_regulation_sub_type")
        or rule_assessment.get("self_regulation_sub_type")
    )
    llm_failure_blocks_intervention = _llm_failure_blocks_intervention(llm_meta)
    source_hint = (rule_state or {}).get("state_source_hint") or rule_assessment.get("state_source_hint")

    fused_state_code = rule_state_code
    fused_state_label = rule_state_label
    risk_level = rule_risk_level
    risk_label = rule_risk_label
    confidence = rule_score
    decision_source = "unknown_fallback"
    assessment_status = "confirmed" if rule_status == "state_detected" and rule_state_code != "unknown" else "insufficient_evidence"
    should_intervene = False

    if llm_valid:
        fused_state_code = llm_state_code
        fused_state_label, risk_level, risk_label = STATE_META[llm_state_code]
        confidence = llm_confidence
        decision_source = "state_llm"
        assessment_status = "confirmed" if llm_state_code != "unknown" else "insufficient_evidence"
        should_intervene = llm_state_code not in {"positive_collaboration", "unknown"}
    elif source_hint == "silence_rule" and rule_state_code == "negative_silence" and rule_status != "insufficient_evidence":
        decision_source = "silence_rule"
        assessment_status = "confirmed"
        should_intervene = True
    elif rule_status != "insufficient_evidence" and rule_state_code != "unknown" and rule_score >= 0.55:
        decision_source = "rule_high_confidence_fallback"
        assessment_status = "uncertain" if llm_failure_blocks_intervention else "confirmed"
        should_intervene = rule_state_code != "positive_collaboration"
    else:
        fused_state_code = "unknown"
        fused_state_label, risk_level, risk_label = STATE_META["unknown"]
        confidence = round(max(rule_score, llm_confidence), 3)
        decision_source = "unknown_fallback"
        assessment_status = "insufficient_evidence"
        should_intervene = False

    if llm_failure_blocks_intervention:
        if rule_state_code != "unknown" and rule_score >= 0.55:
            fused_state_code = rule_state_code
            fused_state_label, risk_level, risk_label = STATE_META[rule_state_code]
            confidence = rule_score
            assessment_status = "uncertain"
        else:
            fused_state_code = "unknown"
            fused_state_label, risk_level, risk_label = STATE_META["unknown"]
            confidence = round(max(rule_score, llm_confidence), 3)
            assessment_status = "insufficient_evidence"
        decision_source = "rule_high_confidence_fallback" if rule_state_code != "unknown" and rule_score >= 0.55 else "unknown_fallback"
        should_intervene = False
    elif fused_state_code == "unknown":
        assessment_status = "insufficient_evidence"
        should_intervene = False
    elif not llm_valid and decision_source == "rule_high_confidence_fallback":
        should_intervene = fused_state_code != "positive_collaboration"
    elif llm_valid:
        should_intervene = fused_state_code not in {"positive_collaboration", "unknown"}
        if llm_valid and llm_payload.get("should_intervene_recommendation") is False:
            should_intervene = False
    if self_regulation_detected and fused_state_code in {
        "conflict_tension",
        "task_detached",
        "blocked_frustration",
        "negative_silence",
    }:
        should_intervene = False

    return {
        "version": FUSION_VERSION,
        "rule_state_code": rule_state_code,
        "rule_score": rule_score,
        "rule_assessment_status": rule_status,
        "llm_state_code": llm_state_code,
        "llm_confidence": llm_confidence,
        "llm_assessment_status": llm_status,
        "fused_state_code": fused_state_code,
        "fused_state_label": fused_state_label,
        "risk_level": risk_level,
        "risk_label": risk_label,
        "confidence": round(confidence, 3),
        "assessment_status": assessment_status,
        "should_intervene": bool(should_intervene),
        "self_regulation_detected": self_regulation_detected,
        "self_regulation_sub_type": self_regulation_sub_type,
        "autonomous_regulation_reason": (
            rule_assessment.get("autonomous_regulation_reason")
            if self_regulation_detected
            else None
        ),
        "decision_source": decision_source,
        "llm_failure_blocks_intervention": llm_failure_blocks_intervention,
        "llm_validation_status": (llm_meta or {}).get("validation_status"),
        "llm_failure_reason": (
            (llm_meta or {}).get("failure_reason")
            or (llm_meta or {}).get("failure_type")
            or (llm_meta or {}).get("schema_error")
        ),
        "llm_secondary_flags": llm_payload.get("secondary_flags") or [],
        "llm_reason": llm_payload.get("reason"),
        "llm_evidence_message_ids": llm_payload.get("evidence_message_ids") or [],
        "llm_evidence_sentences": llm_payload.get("evidence_sentences") or [],
        "rule_legacy_state_code": rule_norm.get("legacy_state_code"),
        "llm_legacy_state_code": llm_payload.get("legacy_state_code"),
        "legacy_state_code": rule_norm.get("legacy_state_code") or llm_payload.get("legacy_state_code"),
        "normalization_reason": rule_norm.get("normalization_reason") or llm_payload.get("normalization_reason"),
        "evidence_tags": sorted(set((rule_norm.get("evidence_tags") or []) + (llm_payload.get("evidence_tags") or []))),
    }


def _build_assessment_evidence(rule_state, fusion_payload):
    parts = [
        f"fusion_version={fusion_payload['version']}",
        f"decision_source={fusion_payload['decision_source']}",
        f"rule_state={fusion_payload['rule_state_code']}",
        f"rule_score={fusion_payload['rule_score']:.2f}",
        f"rule_status={fusion_payload['rule_assessment_status']}",
        f"fused_state={fusion_payload['fused_state_code']}",
        f"assessment_status={fusion_payload['assessment_status']}",
        f"confidence={fusion_payload['confidence']:.2f}",
    ]
    if fusion_payload.get("llm_state_code"):
        parts.extend(
            [
                f"llm_state={fusion_payload['llm_state_code']}",
                f"llm_status={fusion_payload.get('llm_assessment_status')}",
                f"llm_confidence={fusion_payload.get('llm_confidence', 0.0):.2f}",
            ]
        )
    if fusion_payload.get("self_regulation_detected"):
        parts.append("self_regulation_detected=True")
    if fusion_payload.get("llm_reason"):
        parts.append(f"llm_reason={fusion_payload['llm_reason']}")
    if fusion_payload.get("legacy_state_code"):
        parts.append(f"legacy_state_code={fusion_payload['legacy_state_code']}")
    if fusion_payload.get("normalization_reason"):
        parts.append(f"normalization_reason={fusion_payload['normalization_reason']}")
    if fusion_payload.get("evidence_tags"):
        parts.append("evidence_tags=" + ",".join(fusion_payload["evidence_tags"]))
    parts.append((rule_state or {}).get("evidence") or "")
    return "; ".join(part for part in parts if part)


def confirm_group_state_for_assessment(assessment_id):
    """Require consecutive same-state windows before marking the state confirmed."""
    assessment = query_one("SELECT * FROM state_assessments WHERE id=?", (assessment_id,))
    if not assessment:
        return {
            "version": CONFIRMATION_VERSION,
            "confirmed_windows": 0,
            "required_windows": _coerce_confirmation_window_count(),
            "confirmation_status": "insufficient_evidence",
            "confirmed": False,
        }

    required_windows = _coerce_confirmation_window_count()
    if (
        assessment["assessment_status"] == "insufficient_evidence"
        or not assessment["fused_state_code"]
        or assessment["fused_state_code"] == "unknown"
    ):
        return {
            "version": CONFIRMATION_VERSION,
            "confirmed_windows": 0,
            "required_windows": required_windows,
            "confirmation_status": "insufficient_evidence",
            "confirmed": False,
        }

    rows = query_all(
        """
        SELECT id, fused_state_code, assessment_status
        FROM state_assessments
        WHERE group_id=?
          AND ((session_id IS NULL AND ? IS NULL) OR session_id=?)
          AND ((discussion_id IS NULL AND ? IS NULL) OR discussion_id=?)
          AND ((session_no IS NULL AND ? IS NULL) OR session_no=?)
          AND ((task_id IS NULL AND ? IS NULL) OR task_id=?)
          AND id<=?
        ORDER BY id DESC
        LIMIT ?
        """,
        _confirmation_scope_params(assessment) + (required_windows,),
    )

    consecutive = 0
    expected_code = assessment["fused_state_code"]
    for row in rows:
        if row["assessment_status"] == "insufficient_evidence":
            break
        if row["fused_state_code"] != expected_code:
            break
        consecutive += 1

    confirmation_status = "confirmed" if consecutive >= required_windows else "pending_confirmation"
    return {
        "version": CONFIRMATION_VERSION,
        "confirmed_windows": consecutive,
        "required_windows": required_windows,
        "confirmation_status": confirmation_status,
        "confirmed": confirmation_status == "confirmed",
    }


def _update_confirmation_fields(table_name, record_id, confirmation):
    execute(
        f"""
        UPDATE {table_name}
        SET confirmed_windows=?, confirmation_status=?
        WHERE id=?
        """,
        (
            confirmation["confirmed_windows"],
            confirmation["confirmation_status"],
            record_id,
        ),
    )


def _assessment_window_fields(rule_state, created_at):
    context = (rule_state or {}).get("context_json") or {}
    window_start = (
        (rule_state or {}).get("window_start")
        or context.get("window_start")
        or created_at
    )
    window_end = (
        (rule_state or {}).get("window_end")
        or context.get("window_end")
        or created_at
    )
    return window_start, window_end


def _assessment_score_fields(rule_state):
    features = (rule_state or {}).get("feature_json") or {}
    behavior = features.get("behavior") or {}
    text = features.get("text") or {}
    valence_score = _safe_float(
        (rule_state or {}).get("valence_score", text.get("affective_polarity_score")),
        0.0,
    )
    interaction_activation_score = _safe_float(
        (
            (rule_state or {}).get(
                "interaction_activation_score",
                behavior.get("interaction_intensity_score"),
            )
        ),
        0.0,
    )
    return valence_score, interaction_activation_score


def persist_state_assessment(rule_state, llm_result=None, llm_meta=None):
    scope_conn = db()
    try:
        from services.discussion_scope import resolve_discussion_scope

        scope = resolve_discussion_scope(
            scope_conn,
            group_id=rule_state["group_id"],
            session_id=rule_state.get("session_id"),
            session_no=rule_state.get("session_no"),
            task_id=rule_state.get("task_id"),
            discussion_id=rule_state.get("discussion_id")
            or (rule_state.get("context_json") or {}).get("discussion_id"),
            allow_legacy_fallback=False,
        )
    finally:
        scope_conn.close()
    context_json = dict(rule_state.get("context_json") or {})
    context_json["discussion_scope"] = scope.as_dict()
    fusion = fuse_state_evidence(rule_state, llm_result, llm_meta)
    evidence_summary = _build_assessment_evidence(rule_state, fusion)
    analysis_audit = _build_analysis_audit(rule_state, llm_result, llm_meta)
    llm_payload = _build_llm_assessment_payload(llm_result, llm_meta)
    session_id = scope.session_id
    created_at = now_str()
    window_start, window_end = _assessment_window_fields(rule_state, created_at)
    valence_score, interaction_activation_score = _assessment_score_fields(rule_state)
    assessment_id = execute(
        """
        INSERT INTO state_assessments(
            group_id, session_id, task_id, session_no, discussion_id,
            window_start, window_end,
            rule_state_code, llm_state_code, fused_state_code, fused_state_label,
            valence_score, interaction_activation_score,
            assessment_status, confidence, risk_level, risk_label,
            should_intervene, self_regulation_detected, evidence_summary,
            rule_assessment_json, llm_assessment_json, fusion_json,
            context_json, feature_json, rule_version, detector_version,
            model_name, prompt_version, request_started_at, request_finished_at,
            latency_ms, fallback_used, error_message, created_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            rule_state["group_id"],
            session_id,
            scope.task_id,
            scope.session_no,
            scope.discussion_id,
            window_start,
            window_end,
            fusion["rule_state_code"],
            fusion.get("llm_state_code"),
            fusion["fused_state_code"],
            fusion["fused_state_label"],
            valence_score,
            interaction_activation_score,
            fusion["assessment_status"],
            fusion["confidence"],
            fusion["risk_level"],
            fusion["risk_label"],
            1 if fusion["should_intervene"] else 0,
            1 if fusion["self_regulation_detected"] else 0,
            evidence_summary,
            json.dumps(rule_state.get("rule_assessment") or {}, ensure_ascii=False),
            json.dumps(llm_payload, ensure_ascii=False) if llm_payload else None,
            json.dumps(fusion, ensure_ascii=False),
            json.dumps(context_json, ensure_ascii=False),
            json.dumps(rule_state.get("feature_json") or {}, ensure_ascii=False),
            analysis_audit.get("rule_version"),
            analysis_audit.get("detector_version"),
            analysis_audit.get("model_name"),
            analysis_audit.get("prompt_version"),
            analysis_audit.get("request_started_at"),
            analysis_audit.get("request_finished_at"),
            analysis_audit.get("latency_ms"),
            1 if analysis_audit.get("fallback_used") else 0,
            analysis_audit.get("error_message"),
            created_at,
        ),
    )
    confirmation = confirm_group_state_for_assessment(assessment_id)
    _update_confirmation_fields("state_assessments", assessment_id, confirmation)

    group_state_id = execute(
        """
        INSERT INTO group_states(
            group_id, state_code, state_label, risk_level, risk_label, evidence,
            task_id, session_no, session_id, discussion_id,
            context_json, feature_json, state_score, rule_assessment_json,
            state_assessment_id, assessment_status, confirmed_windows, confirmation_status,
            llm_state_code, fusion_json, created_at
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            rule_state["group_id"],
            fusion["fused_state_code"],
            fusion["fused_state_label"],
            fusion["risk_level"],
            fusion["risk_label"],
            evidence_summary,
            scope.task_id,
            scope.session_no,
            scope.session_id,
            scope.discussion_id,
            json.dumps(context_json, ensure_ascii=False),
            json.dumps(rule_state.get("feature_json") or {}, ensure_ascii=False),
            fusion["confidence"],
            json.dumps(rule_state.get("rule_assessment") or {}, ensure_ascii=False),
            assessment_id,
            fusion["assessment_status"],
            confirmation["confirmed_windows"],
            confirmation["confirmation_status"],
            fusion.get("llm_state_code"),
            json.dumps(fusion, ensure_ascii=False),
            now_str(),
        ),
    )
    return {
        "assessment_id": assessment_id,
        "group_state_id": group_state_id,
        "fusion": fusion,
        "confirmation": confirmation,
        "evidence_summary": evidence_summary,
    }


def upgrade_state_assessment_with_llm(rule_state, llm_result=None, llm_meta=None):
    assessment_id = (rule_state or {}).get("state_assessment_id")
    group_state_id = (rule_state or {}).get("group_state_id")
    if not assessment_id:
        return persist_state_assessment(rule_state, llm_result, llm_meta)

    fusion = fuse_state_evidence(rule_state, llm_result, llm_meta)
    evidence_summary = _build_assessment_evidence(rule_state, fusion)
    analysis_audit = _build_analysis_audit(rule_state, llm_result, llm_meta)
    llm_payload = _build_llm_assessment_payload(llm_result, llm_meta)
    execute(
        """
        UPDATE state_assessments
        SET llm_state_code=?, fused_state_code=?, fused_state_label=?,
            assessment_status=?, confidence=?, risk_level=?, risk_label=?,
            should_intervene=?, self_regulation_detected=?, evidence_summary=?,
            llm_assessment_json=?, fusion_json=?, context_json=?, feature_json=?,
            rule_version=?, detector_version=?, model_name=?, prompt_version=?,
            request_started_at=?, request_finished_at=?, latency_ms=?,
            fallback_used=?, error_message=?
        WHERE id=?
        """,
        (
            fusion.get("llm_state_code"),
            fusion["fused_state_code"],
            fusion["fused_state_label"],
            fusion["assessment_status"],
            fusion["confidence"],
            fusion["risk_level"],
            fusion["risk_label"],
            1 if fusion["should_intervene"] else 0,
            1 if fusion["self_regulation_detected"] else 0,
            evidence_summary,
            json.dumps(llm_payload, ensure_ascii=False) if llm_payload else None,
            json.dumps(fusion, ensure_ascii=False),
            json.dumps(rule_state.get("context_json") or {}, ensure_ascii=False),
            json.dumps(rule_state.get("feature_json") or {}, ensure_ascii=False),
            analysis_audit.get("rule_version"),
            analysis_audit.get("detector_version"),
            analysis_audit.get("model_name"),
            analysis_audit.get("prompt_version"),
            analysis_audit.get("request_started_at"),
            analysis_audit.get("request_finished_at"),
            analysis_audit.get("latency_ms"),
            1 if analysis_audit.get("fallback_used") else 0,
            analysis_audit.get("error_message"),
            assessment_id,
        ),
    )
    confirmation = confirm_group_state_for_assessment(assessment_id)
    _update_confirmation_fields("state_assessments", assessment_id, confirmation)

    if group_state_id:
        execute(
            """
            UPDATE group_states
            SET state_code=?, state_label=?, risk_level=?, risk_label=?,
                evidence=?, state_score=?, rule_assessment_json=?,
                assessment_status=?, confirmed_windows=?, confirmation_status=?,
                llm_state_code=?, fusion_json=?
            WHERE id=?
            """,
            (
                fusion["fused_state_code"],
                fusion["fused_state_label"],
                fusion["risk_level"],
                fusion["risk_label"],
                evidence_summary,
                fusion["confidence"],
                json.dumps(rule_state.get("rule_assessment") or {}, ensure_ascii=False),
                fusion["assessment_status"],
                confirmation["confirmed_windows"],
                confirmation["confirmation_status"],
                fusion.get("llm_state_code"),
                json.dumps(fusion, ensure_ascii=False),
                group_state_id,
            ),
        )

    return {
        "assessment_id": assessment_id,
        "group_state_id": group_state_id,
        "fusion": fusion,
        "confirmation": confirmation,
        "evidence_summary": evidence_summary,
    }
