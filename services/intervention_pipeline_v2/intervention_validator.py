# -*- coding: utf-8 -*-
'''Intervention pre-validation service.'''
import json
from datetime import datetime, timedelta
from db import query_one
from config import INTERVENTION_V2_MAX_DELTA_STALE, INTERVENTION_V2_COOLDOWN_SECONDS
from services.help_request_coverage_service import HelpRequestCoverageService
from services.intervention_pipeline_v2.strategy_service import FORMAL_INTERVENTION_STATES


STUDENT_HELP_TRIGGER_SOURCE = "student_help_request"
SILENCE_TRIGGER_SOURCES = {"silence_check", "silence_rule"}


def _monitor_trigger_source(monitor_run=None):
    if not monitor_run:
        return None
    try:
        return monitor_run.get("trigger_source") or monitor_run.get("trigger_type")
    except AttributeError:
        return None


def _monitor_scope_value(monitor_run, key):
    if not monitor_run:
        return None
    try:
        return monitor_run.get(key)
    except AttributeError:
        try:
            return monitor_run[key]
        except (IndexError, KeyError, TypeError):
            return None


class InterventionValidator:

    @staticmethod
    def validate(group_id, cutoff_sequence, monitor_run=None):
        state_check = InterventionValidator._check_room_state(group_id)
        if not state_check.get("ok"):
            return {"valid": False, "action": "REJECT", "reason": state_check.get("reason"),
                    "state_check": state_check, "cutoff_check": {}, "cooldown_check": {},
                    "active_run_check": {}, "help_request_check": {}, "triggerable_state_check": {}}

        triggerable_state_check = InterventionValidator._check_triggerable_state(monitor_run)
        cutoff_check = InterventionValidator._check_cutoff_sequence(
            group_id,
            cutoff_sequence,
            monitor_run,
        )
        cooldown_check = InterventionValidator._check_cooldown(group_id, monitor_run)
        active_run_check = InterventionValidator._check_active_intervention(
            group_id,
            monitor_run,
        )
        help_request_check = InterventionValidator._check_pending_help_requests(group_id, monitor_run)

        issues = []
        for check, name in [(triggerable_state_check, "triggerable_state"), (cutoff_check, "cutoff"), (cooldown_check, "cooldown"),
                            (active_run_check, "active"), (help_request_check, "help")]:
            if not check.get("ok"):
                issues.append(check.get("reason", name + "_failed"))

        delta = cutoff_check.get("delta")
        if delta is not None and delta > INTERVENTION_V2_MAX_DELTA_STALE:
            return {"valid": False, "action": "STALE", "reason": f"delta={delta}",
                    "delta": delta, "state_check": state_check, "cutoff_check": cutoff_check,
                    "cooldown_check": cooldown_check, "active_run_check": active_run_check,
                    "help_request_check": help_request_check, "triggerable_state_check": triggerable_state_check}

        if issues:
            return {"valid": False, "action": "REJECT", "reason": "; ".join(issues),
                    "delta": delta, "state_check": state_check, "cutoff_check": cutoff_check,
                    "cooldown_check": cooldown_check, "active_run_check": active_run_check,
                    "help_request_check": help_request_check, "triggerable_state_check": triggerable_state_check}

        action = "INTERVENE" if delta == 0 else "QUICK_RECHECK"
        return {"valid": True, "action": action, "delta": delta, "reason": None,
                "state_check": state_check, "cutoff_check": cutoff_check,
                "cooldown_check": cooldown_check, "active_run_check": active_run_check,
                "help_request_check": help_request_check, "triggerable_state_check": triggerable_state_check,
                "trigger_source": _monitor_trigger_source(monitor_run) or "auto_v2"}

    @staticmethod
    def _check_room_state(group_id):
        row = query_one("SELECT id, state, lock_token, lock_expires_at FROM groups WHERE id=?", (group_id,))
        if not row:
            return {"ok": False, "reason": "room_not_found"}
        if row["state"] != "OPEN":
            return {"ok": False, "reason": "state_" + row["state"]}
        return {"ok": True, "state": row["state"]}

    @staticmethod
    def _check_cutoff_sequence(group_id, cutoff_sequence, monitor_run=None):
        trigger_source = _monitor_trigger_source(monitor_run)
        if trigger_source in SILENCE_TRIGGER_SOURCES:
            try:
                session_id = (monitor_run or {}).get("session_id")
            except AttributeError:
                session_id = None
            row = query_one(
                """
                SELECT sequence AS last_message_sequence
                FROM messages
                WHERE group_id=? AND sequence IS NOT NULL
                  AND COALESCE(role, '')='student'
                  AND (? IS NULL OR session_id=?)
                ORDER BY sequence DESC, id DESC
                LIMIT 1
                """,
                (group_id, session_id, session_id),
            )
            cutoff_scope = "latest_student_message"
        else:
            try:
                session_id = (monitor_run or {}).get("session_id")
            except AttributeError:
                session_id = None
            # Fixed-schedule emotion messages are independent of strategy
            # review and must not make a student-anchored assessment stale.
            row = query_one(
                """
                SELECT sequence AS last_message_sequence
                FROM messages
                WHERE group_id=? AND sequence IS NOT NULL
                  AND (? IS NULL OR session_id=?)
                  AND NOT (
                      COALESCE(role, '')='agent'
                      AND COALESCE(agent_type, '')='emotion'
                  )
                ORDER BY sequence DESC, id DESC
                LIMIT 1
                """,
                (group_id, session_id, session_id),
            )
            cutoff_scope = "latest_non_emotion_message"
        if not row and trigger_source == STUDENT_HELP_TRIGGER_SOURCE:
            row = query_one(
                """
                SELECT last_message_sequence
                FROM groups
                WHERE id=?
                """,
                (group_id,),
            )
            cutoff_scope = "student_help_group_sequence_fallback"
        if not row:
            return {
                "ok": False,
                "reason": (
                    "no_student_message_for_silence"
                    if trigger_source in SILENCE_TRIGGER_SOURCES
                    else "room_not_found"
                ),
                "delta": None,
                "cutoff_scope": cutoff_scope,
                "trigger_source": trigger_source,
            }
        current_seq = int(row["last_message_sequence"] or 0)
        delta = current_seq - cutoff_sequence
        if delta < 0:
            return {"ok": False, "reason": f"cutoff={cutoff_sequence} > current={current_seq}", "delta": delta}
        return {
            "ok": True,
            "delta": delta,
            "cutoff_sequence": cutoff_sequence,
            "current_sequence": current_seq,
            "cutoff_scope": cutoff_scope,
            "trigger_source": trigger_source,
        }

    @staticmethod
    def _check_triggerable_state(monitor_run=None):
        if not monitor_run:
            return {"ok": True, "state_code": None, "trigger_source": None}
        try:
            state_code = monitor_run.get("final_state") or monitor_run.get("detected_state") or "unknown"
        except AttributeError:
            state_code = "unknown"
        trigger_source = _monitor_trigger_source(monitor_run)
        if state_code not in FORMAL_INTERVENTION_STATES:
            return {
                "ok": False,
                "reason": f"non_intervention_state_{state_code}",
                "state_code": state_code,
                "trigger_source": trigger_source,
                "formal_intervention_states": sorted(FORMAL_INTERVENTION_STATES),
            }
        return {
            "ok": True,
            "state_code": state_code,
            "trigger_source": trigger_source,
            "formal_intervention_states": sorted(FORMAL_INTERVENTION_STATES),
        }

    @staticmethod
    def _check_cooldown(group_id, monitor_run=None):
        trigger_source = _monitor_trigger_source(monitor_run)
        if trigger_source == STUDENT_HELP_TRIGGER_SOURCE:
            return {
                "ok": True,
                "cooling": False,
                "bypassed_by": STUDENT_HELP_TRIGGER_SOURCE,
                "cooldown_seconds": int(INTERVENTION_V2_COOLDOWN_SECONDS),
            }
        session_id = _monitor_scope_value(monitor_run, "session_id")
        since = (datetime.now() - timedelta(seconds=INTERVENTION_V2_COOLDOWN_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
        row = query_one(
            """
            SELECT COUNT(*) AS c FROM intervention_runs
            WHERE group_id=? AND created_at>=?
              AND (? IS NULL OR session_id=?)
              AND COALESCE(agent_type, 'strategy')='strategy'
              AND status IN ('PUBLISHED','FALLBACK')
            """,
            (group_id, since, session_id, session_id),
        )
        recent_count = int(row["c"]) if row else 0
        if recent_count > 0:
            return {
                "ok": False,
                "reason": f"cooldown_active_{recent_count}",
                "cooling": True,
                "cooldown_seconds": int(INTERVENTION_V2_COOLDOWN_SECONDS),
            }
        return {"ok": True, "cooling": False, "cooldown_seconds": int(INTERVENTION_V2_COOLDOWN_SECONDS)}

    @staticmethod
    def _check_active_intervention(group_id, monitor_run=None):
        session_id = _monitor_scope_value(monitor_run, "session_id")
        active = query_one(
            """
            SELECT id, status FROM intervention_runs
            WHERE group_id=?
              AND (? IS NULL OR session_id=?)
              AND COALESCE(agent_type, 'strategy')='strategy'
              AND status NOT IN ('PUBLISHED','FALLBACK','PASS','SKIPPED','FAILED','EXPIRED','CANCELLED','STALE')
            ORDER BY id DESC LIMIT 1
            """,
            (group_id, session_id, session_id),
        )
        if active:
            return {"ok": False, "reason": f"active_run_{active['id']}_{active['status']}", "active_run_id": active["id"]}
        return {"ok": True, "active_run_id": None}

    @staticmethod
    def _check_pending_help_requests(group_id, monitor_run=None, window_seconds=30):
        trigger_source = _monitor_trigger_source(monitor_run)
        if trigger_source == STUDENT_HELP_TRIGGER_SOURCE:
            guard = HelpRequestCoverageService.bypassed(
                STUDENT_HELP_TRIGGER_SOURCE
            )
            return {
                **guard,
                "ok": True,
                "pending_count": 0,
                "bypassed_by": STUDENT_HELP_TRIGGER_SOURCE,
            }
        monitor_run = monitor_run or {}
        evidence_sequences = []
        try:
            parsed_evidence = json.loads(
                monitor_run.get("evidence_sequences_json") or "[]"
            )
            if isinstance(parsed_evidence, list):
                evidence_sequences = sorted(
                    {
                        int(value)
                        for value in parsed_evidence
                        if not isinstance(value, bool)
                    }
                )
        except (TypeError, ValueError):
            evidence_sequences = []
        cutoff = (
            monitor_run.get("target_end_sequence")
            or (max(evidence_sequences) if evidence_sequences else None)
            or monitor_run.get("cutoff_sequence")
        )
        start = (
            monitor_run.get("target_start_sequence")
            or (min(evidence_sequences) if evidence_sequences else None)
            or monitor_run.get("context_from_sequence")
            or cutoff
        )
        if cutoff is None or start is None:
            guard = HelpRequestCoverageService.bypassed("no_target_sequence")
            return {
                **guard,
                "ok": True,
                "pending_count": 0,
                "scope": "no_target_sequence",
            }
        segment_id = monitor_run.get("target_segment_id")
        try:
            audit_payload = json.loads(monitor_run.get("rule_result_json") or "{}")
        except (TypeError, ValueError):
            audit_payload = {}
        if segment_id is None and isinstance(audit_payload, dict):
            segment_id = (audit_payload.get("monitor_audit") or {}).get(
                "segment_id"
            )
        guard = HelpRequestCoverageService.evaluate(
            group_id,
            monitor_run.get("session_id"),
            monitor_run.get("final_state") or monitor_run.get("detected_state"),
            segment_id,
            start,
            cutoff,
            datetime.now(),
        )
        return {
            **guard,
            "ok": not guard["blocked"],
            "reason": guard.get("reason_code") if guard["blocked"] else None,
            "pending_count": len(guard.get("help_request_ids") or []),
        }
