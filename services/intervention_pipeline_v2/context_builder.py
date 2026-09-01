# -*- coding: utf-8 -*-

"""构建 LLM 介入上下文??。



上下文至少包含：

- 讨论题目与要求；

- 当前任务阶段；

- 最??10-20 条消息；

- 小组长期摘要（如已有）；

- 参与统计；

- monitor_run 的状态、置信度、依据；

- 最近介入记录；

- 最??3 个候选策略??

"""

import json

from datetime import datetime, timedelta

from typing import Optional



from db import query_one, query_all, now_str, parse_dt, get_current_learning_task

from config import (

    INTERVENTION_V2_MAX_MESSAGES_FOR_CONTEXT,

    INTERVENTION_V2_MIN_MESSAGES_FOR_CONTEXT,

    INTERVENTION_V2_MAX_CANDIDATE_STRATEGIES,

)


STRATEGY_CONTEXT_MAX_MESSAGES = 40
STRATEGY_CONTEXT_MAX_CHARS = 8000
STRATEGY_CONTEXT_MAX_MESSAGE_CHARS = 500

STRATEGY_BOUNDARY_PUSH_MODES = {
    "sera_auto",
    "sera_auto_v2",
    "student_request",
    "sera_teacher_confirmed",
}

STRATEGY_BOUNDARY_TRIGGER_SOURCES = {
    "auto",
    "auto_v2",
    "new_message",
    "student_help_request",
    "teacher_manual",
    "teacher_confirmed",
}




class ContextBuilder:

    """V2 介入上下文构建器。"""



    @staticmethod

    def build(group_id: int, monitor_run: dict, candidate_strategies: list = None) -> dict:

        """构建完整??LLM 上下文。"""

        context = {

            "group_id": group_id,

            "task_info": ContextBuilder._get_task_info(group_id),

            "room_info": ContextBuilder._get_room_info(group_id),

            "recent_messages": ContextBuilder._get_recent_messages(group_id),

            "trigger_window": ContextBuilder._get_trigger_window(group_id, monitor_run),

            "participation_stats": ContextBuilder._get_participation_stats(group_id),

            "monitor_run": ContextBuilder._summarize_monitor_run(monitor_run),

            "recent_interventions": ContextBuilder._get_recent_interventions(group_id),

            "candidate_strategies": candidate_strategies or [],

        }

        return context



    @staticmethod
    def build_strategy_review_context(
        group_id: int,
        session_id: int,
        monitor_run_id: int,
        cutoff_sequence: int,
        candidate_strategies: list = None,
        rule_candidate: dict = None,
        state_assessment_id: int = None,
        state_assessment: dict = None,
        trigger_source: str = "auto_state",
    ) -> dict:
        """Build the automatic strategy-decision context without LLM I/O."""
        monitor_run = ContextBuilder._get_monitor_run(monitor_run_id)
        session_context = ContextBuilder._get_strategy_session_context(session_id)
        state_assessment = ContextBuilder._summarize_state_assessment(
            state_assessment
            or ContextBuilder._get_state_assessment(state_assessment_id)
            or ContextBuilder._get_state_assessment_from_monitor_audit(monitor_run),
            monitor_run=monitor_run,
        )
        rule_candidate = rule_candidate or ContextBuilder._rule_candidate_from_monitor_run(
            monitor_run,
            cutoff_sequence,
        )
        previous = ContextBuilder.find_previous_strategy_intervention(
            group_id,
            session_id,
            cutoff_sequence,
        )
        if previous and previous.get("sequence") is not None:
            from_sequence = int(previous["sequence"]) + 1
        else:
            from_sequence = ContextBuilder._first_student_sequence(
                group_id,
                session_context,
                cutoff_sequence,
            )

        raw_messages = ContextBuilder._get_strategy_context_messages(
            group_id,
            session_context,
            from_sequence,
            cutoff_sequence,
        )
        assessment_evidence_sequences = set(state_assessment.get("evidence_message_ids") or [])
        assessment_evidence_sequences.update(
            ContextBuilder._extract_rule_evidence_sequences(rule_candidate)
        )
        messages, truncated, omitted_ranges = ContextBuilder._apply_strategy_context_limits(
            raw_messages,
            assessment_evidence_sequences,
        )

        state_code = (
            state_assessment.get("detected_state")
            or rule_candidate.get("state_code")
            or (monitor_run or {}).get("final_state")
        )
        allowed = candidate_strategies
        if allowed is None:
            from services.intervention_pipeline_v2.strategy_service import StrategyService
            allowed = StrategyService.find_strategies_for_state(state_code)
        allowed = ContextBuilder._summarize_allowed_strategies(allowed or [])

        session_info = session_context.get("session") or {}
        task_info = session_context.get("task") or {}
        if task_info is not None:
            task_info = dict(task_info)
            task_info["document_summary"] = ContextBuilder._get_document_summary(
                group_id,
                session_context,
            )
        input_sequences = [
            msg["sequence"] for msg in messages
            if msg.get("sequence") is not None
        ]
        return {
            "group_id": group_id,
            "session_id": session_id,
            "monitor_run_id": monitor_run_id,
            "state_assessment_id": state_assessment.get("id"),
            "task_context": {
                "session": session_info,
                "task": task_info,
            },
            "session": session_info,
            "task": task_info,
            "state_assessment": state_assessment,
            "confirmed_state": state_assessment,
            "rule_candidate": rule_candidate,
            "previous_strategy_intervention": previous,
            "context_boundary": {
                "previous_strategy_sequence": previous.get("sequence") if previous else None,
                "from_sequence": from_sequence,
                "to_sequence": cutoff_sequence,
                "context_truncated": truncated,
                "omitted_sequence_ranges": omitted_ranges,
            },
            "context_from_sequence": from_sequence,
            "context_to_sequence": cutoff_sequence,
            "messages": messages,
            "input_message_sequences": input_sequences,
            "runtime_context": ContextBuilder._get_strategy_runtime_context(
                group_id,
                session_context,
                messages,
                trigger_source=trigger_source,
                state_code=state_code,
                monitor_run_id=monitor_run_id,
                cutoff_sequence=cutoff_sequence,
            ),
            "allowed_strategies": allowed,
            "context_truncated": truncated,
            "omitted_sequence_ranges": omitted_ranges,
        }


    @staticmethod
    def find_previous_strategy_intervention(
        group_id: int,
        session_id: int,
        cutoff_sequence: int,
    ) -> dict:
        """Return the latest published strategy-type message before cutoff."""
        session_context = ContextBuilder._get_strategy_session_context(session_id)
        session_sql, session_params = ContextBuilder._message_session_filter(
            "m",
            session_context,
        )
        push_placeholders = ",".join("?" for _ in STRATEGY_BOUNDARY_PUSH_MODES)
        trigger_placeholders = ",".join("?" for _ in STRATEGY_BOUNDARY_TRIGGER_SOURCES)
        params = [
            group_id,
            cutoff_sequence,
            *session_params,
            *sorted(STRATEGY_BOUNDARY_PUSH_MODES),
            *sorted(STRATEGY_BOUNDARY_TRIGGER_SOURCES),
        ]
        row = query_one(
            f"""
            SELECT m.id, m.sequence, m.content, m.created_at, m.agent_type,
                   m.strategy_id AS message_strategy_id,
                   il.push_mode, il.trigger_source, il.strategy_id AS log_strategy_id,
                   il.pushed_by_user_id,
                   ir.status AS run_status, ir.agent_type AS run_agent_type,
                   ir.trigger_type AS run_trigger_type,
                   hr.id AS help_request_id
            FROM messages m
            LEFT JOIN intervention_logs il ON il.id = m.linked_log_id
            LEFT JOIN intervention_runs ir
              ON ir.id = m.intervention_run_id
              OR (il.intervention_id IS NOT NULL AND ir.id = il.intervention_id)
            LEFT JOIN agent_suggestions ags ON ags.id = il.suggestion_id
            LEFT JOIN help_requests hr
              ON hr.id = il.help_request_id
              OR hr.id = ags.help_request_id
              OR (ir.id IS NOT NULL AND hr.intervention_run_id = ir.id)
            WHERE m.group_id=?
              AND m.sequence IS NOT NULL
              AND m.sequence<=?
              AND {session_sql}
              AND COALESCE(m.role, '')='agent'
              AND COALESCE(m.agent_type, '') <> 'emotion'
              AND (
                    COALESCE(m.agent_type, '')='strategy'
                 OR (
                    (m.agent_type IS NULL OR TRIM(m.agent_type)='')
                    AND (
                         (il.id IS NOT NULL AND (
                              il.push_mode IN ({push_placeholders})
                           OR il.trigger_source IN ({trigger_placeholders})
                           OR il.help_request_id IS NOT NULL
                           OR ags.help_request_id IS NOT NULL
                         ))
                      OR (ir.id IS NOT NULL
                          AND COALESCE(ir.agent_type, 'strategy')='strategy'
                          AND UPPER(COALESCE(ir.status, '')) IN ('PUBLISHED', 'FALLBACK'))
                      OR hr.id IS NOT NULL
                    )
                 )
              )
              AND (
                    ir.id IS NULL
                 OR ir.status IS NULL
                 OR UPPER(ir.status) NOT IN ('FAILED', 'CANCELLED', 'EXPIRED', 'STALE', 'SKIPPED')
              )
            ORDER BY m.sequence DESC, m.id DESC
            LIMIT 1
            """,
            tuple(params),
        )
        if not row:
            return None
        return {
            "message_id": row["id"],
            "sequence": row["sequence"],
            "source": ContextBuilder._strategy_boundary_source(dict(row)),
            "strategy_id": row["message_strategy_id"] or row["log_strategy_id"],
            "message": row["content"],
            "created_at": row["created_at"],
        }


    @staticmethod
    def _strategy_boundary_source(row: dict) -> str:
        push_mode = row.get("push_mode")
        trigger_source = row.get("trigger_source") or row.get("run_trigger_type")
        if row.get("help_request_id") or push_mode == "student_request" or trigger_source == "student_help_request":
            return "student_request"
        if push_mode == "sera_teacher_confirmed" or row.get("pushed_by_user_id"):
            return "teacher_manual"
        return "auto"


    @staticmethod
    def _get_monitor_run(monitor_run_id: int) -> dict:
        if not monitor_run_id:
            return {}
        row = query_one("SELECT * FROM monitor_runs WHERE id=?", (monitor_run_id,))
        return dict(row) if row else {}


    @staticmethod
    def _get_state_assessment(state_assessment_id: int) -> dict:
        if not state_assessment_id:
            return {}
        row = query_one("SELECT * FROM state_assessments WHERE id=?", (state_assessment_id,))
        return dict(row) if row else {}


    @staticmethod
    def _get_state_assessment_from_monitor_audit(monitor_run: dict) -> dict:
        audit = (
            ContextBuilder._json_value((monitor_run or {}).get("rule_result_json"), {}) or {}
        ).get("monitor_audit") or {}
        assessment_id = audit.get("state_assessment_id")
        return ContextBuilder._get_state_assessment(assessment_id)


    @staticmethod
    def _assessment_segment_evidence(assessment_id: int) -> list:
        if not assessment_id:
            return []
        row = query_one(
            """
            SELECT evidence_message_ids_json
            FROM collaboration_state_segments
            WHERE assessment_id=? AND source='state_monitor'
            ORDER BY id DESC LIMIT 1
            """,
            (assessment_id,),
        )
        if not row:
            return []
        parsed = ContextBuilder._json_value(row["evidence_message_ids_json"], [])
        if not isinstance(parsed, list):
            return []
        result = []
        for item in parsed:
            try:
                result.append(int(item))
            except (TypeError, ValueError):
                continue
        return result


    @staticmethod
    def _summarize_state_assessment(assessment: dict, monitor_run: dict = None) -> dict:
        assessment = dict(assessment or {})
        monitor_run = monitor_run or {}
        fusion = ContextBuilder._json_value(assessment.get("fusion_json"), {}) or {}
        rule_assessment = ContextBuilder._json_value(assessment.get("rule_assessment_json"), {}) or {}
        context_json = ContextBuilder._json_value(assessment.get("context_json"), {}) or {}

        evidence = []
        for source in (
            fusion.get("llm_evidence_message_ids"),
            fusion.get("evidence_message_ids"),
            fusion.get("evidence_sequences"),
            rule_assessment.get("evidence_message_ids"),
            rule_assessment.get("evidence_sequences"),
            context_json.get("evidence_message_ids"),
            context_json.get("evidence_sequences"),
            ContextBuilder._assessment_segment_evidence(assessment.get("id")),
        ):
            if not isinstance(source, list):
                continue
            for item in source:
                try:
                    value = int(item)
                except (TypeError, ValueError):
                    continue
                if value not in evidence:
                    evidence.append(value)

        detected_state = (
            assessment.get("fused_state_code")
            or assessment.get("state_code")
            or monitor_run.get("final_state")
        )
        confidence = assessment.get("confidence")
        if confidence is None:
            confidence = monitor_run.get("confidence")
        return {
            "id": assessment.get("id"),
            "detected_state": detected_state,
            "confidence": confidence,
            "evidence_message_ids": evidence,
            "reason": (
                assessment.get("evidence_summary")
                or fusion.get("llm_reason")
                or rule_assessment.get("reason")
                or rule_assessment.get("evidence")
            ),
            "source": fusion.get("decision_source") or assessment.get("source"),
            "assessment_status": assessment.get("assessment_status"),
            "confirmation_status": assessment.get("confirmation_status"),
            "should_intervene": bool(assessment.get("should_intervene")),
            "window_start": assessment.get("window_start"),
            "window_end": assessment.get("window_end"),
            "session_id": assessment.get("session_id"),
            "session_no": assessment.get("session_no"),
            "task_id": assessment.get("task_id"),
        }


    @staticmethod
    def _json_value(value, default=None):
        if value in (None, ""):
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return default


    @staticmethod
    def _none_if_blank(value):
        if isinstance(value, str):
            value = value.strip()
            return value or None
        if isinstance(value, list) and not value:
            return None
        return value


    @staticmethod
    def _get_strategy_session_context(session_id: int) -> dict:
        row = query_one(
            """
            SELECT s.id AS session_id, s.session_no, s.session_role, s.status,
                   s.title AS session_title, s.task_id,
                   t.id AS task_id, t.title AS task_title,
                   t.description AS task_description,
                   t.question AS task_question,
                   t.task_goal, t.output_requirement,
                   t.key_concepts_json, t.expected_dimensions_json,
                   t.task_payload_json
            FROM experiment_sessions s
            LEFT JOIN learning_tasks t ON t.id = s.task_id
            WHERE s.id=?
            LIMIT 1
            """,
            (session_id,),
        )
        if not row:
            return {
                "session": {
                    "session_id": session_id,
                    "session_no": None,
                    "session_name": None,
                    "phase": None,
                    "status": None,
                },
                "task": None,
            }
        data = dict(row)
        task_payload = ContextBuilder._json_value(data.get("task_payload_json"), {}) or {}
        key_concepts = ContextBuilder._none_if_blank(
            ContextBuilder._json_value(data.get("key_concepts_json"), None)
        )
        expected_dimensions = ContextBuilder._none_if_blank(ContextBuilder._json_value(
            data.get("expected_dimensions_json"),
            None,
        ))
        session_no = data.get("session_no")
        session_name = data.get("session_title") or (
            f"Session {session_no}" if session_no is not None else None
        )
        task = None
        if data.get("task_id"):
            task = {
                "task_id": data.get("task_id"),
                "title": ContextBuilder._none_if_blank(data.get("task_title")),
                "topic": ContextBuilder._none_if_blank(data.get("task_title")),
                "description": ContextBuilder._none_if_blank(data.get("task_description")),
                "goal": ContextBuilder._none_if_blank(data.get("task_goal")),
                "question": ContextBuilder._none_if_blank(data.get("task_question")),
                "output_requirement": ContextBuilder._none_if_blank(data.get("output_requirement")),
                "key_concepts": key_concepts,
                "evaluation_criteria": ContextBuilder._none_if_blank(task_payload.get("evaluation_criteria")),
                "expected_dimensions": expected_dimensions,
            }
        return {
            "session": {
                "session_id": data.get("session_id"),
                "session_no": session_no,
                "session_name": session_name,
                "phase": data.get("session_role"),
                "status": data.get("status"),
            },
            "task": task,
        }


    @staticmethod
    def _message_session_filter(alias: str, session_context: dict) -> tuple:
        session = (session_context or {}).get("session") or {}
        session_id = session.get("session_id")
        session_no = session.get("session_no")
        if session_id is not None:
            return (f"{alias}.session_id=?", [session_id])
        if session_no is not None:
            return (f"{alias}.session_no=?", [session_no])
        return ("1=1", [])


    @staticmethod
    def _first_student_sequence(group_id: int, session_context: dict, cutoff_sequence: int) -> int:
        session_sql, session_params = ContextBuilder._message_session_filter(
            "m",
            session_context,
        )
        row = query_one(
            f"""
            SELECT m.sequence
            FROM messages m
            WHERE m.group_id=?
              AND m.sequence IS NOT NULL
              AND (? IS NULL OR m.sequence<=?)
              AND {session_sql}
              AND COALESCE(m.role, '')='student'
            ORDER BY m.sequence ASC, m.id ASC
            LIMIT 1
            """,
            (group_id, cutoff_sequence, cutoff_sequence, *session_params),
        )
        return row["sequence"] if row else None


    @staticmethod
    def _get_strategy_context_messages(
        group_id: int,
        session_context: dict,
        from_sequence: int,
        cutoff_sequence: int,
    ) -> list:
        """Return strategy-review context without fixed-schedule emotion output."""
        session_sql, session_params = ContextBuilder._message_session_filter(
            "m",
            session_context,
        )
        rows = query_all(
            f"""
            SELECT m.id, m.group_id, m.sequence, m.content, m.created_at,
                   m.role, m.sender_type, m.agent_type,
                   m.user_id, m.strategy_id, m.linked_log_id, m.intervention_run_id,
                   m.session_id, m.session_no, m.task_id,
                   COALESCE(NULLIF(TRIM(u.real_name), ''), u.username, 'member') AS display_name
            FROM messages m
            LEFT JOIN users u ON u.id = m.user_id
            WHERE m.group_id=?
              AND m.sequence IS NOT NULL
              AND (? IS NULL OR m.sequence>=?)
              AND (? IS NULL OR m.sequence<=?)
              AND {session_sql}
              AND COALESCE(m.role, 'student') IN ('student', 'agent', 'system', 'teacher')
              AND NOT (
                    COALESCE(m.role, '')='agent'
                AND COALESCE(m.agent_type, '')='emotion'
              )
            ORDER BY m.sequence ASC, m.id ASC
            """,
            (
                group_id,
                from_sequence,
                from_sequence,
                cutoff_sequence,
                cutoff_sequence,
                *session_params,
            ),
        )
        messages = []
        for row in rows:
            role = row["role"] or row["sender_type"] or "student"
            agent_type = row["agent_type"] if role == "agent" else None
            sender_type = row["sender_type"] or role
            if role == "agent":
                if agent_type == "strategy":
                    sender_type = "strategy_agent"
                elif agent_type == "emotion":
                    sender_type = "emotion_agent"
                else:
                    sender_type = "agent"
            messages.append({
                "id": row["id"],
                "message_id": row["sequence"],
                "group_id": row["group_id"],
                "sequence": row["sequence"],
                "role": role,
                "speaker": row["display_name"] if role in {"student", "teacher"} else "SERA",
                "sender": row["display_name"] if role in {"student", "teacher"} else "SERA",
                "sender_type": sender_type,
                "agent_type": agent_type,
                "content": row["content"] or "",
                "time": row["created_at"],
                "created_at": row["created_at"],
                "session_id": row["session_id"],
                "session_no": row["session_no"],
                "task_id": row["task_id"],
                "strategy_id": row["strategy_id"],
                "linked_log_id": row["linked_log_id"],
                "intervention_run_id": row["intervention_run_id"],
                "can_be_state_evidence": role == "student",
            })
        return messages


    @staticmethod
    def _rule_candidate_from_monitor_run(monitor_run: dict, cutoff_sequence: int) -> dict:
        monitor_run = monitor_run or {}
        rule_result = ContextBuilder._json_value(monitor_run.get("rule_result_json"), {}) or {}
        signals = rule_result.get("signals")
        if isinstance(signals, dict):
            signals = [key for key, value in signals.items() if value]
        elif signals is None:
            signals = []
        state_code = (
            rule_result.get("winning_state_code")
            or rule_result.get("state_code")
            or monitor_run.get("final_state")
        )
        score = (
            rule_result.get("winning_score")
            or rule_result.get("score")
            or monitor_run.get("confidence")
        )
        return {
            "state_code": state_code,
            "score": score,
            "signals": signals,
            "trigger_sequence": rule_result.get("trigger_sequence") or cutoff_sequence,
            "rule_result": rule_result,
        }


    @staticmethod
    def _extract_rule_evidence_sequences(rule_candidate) -> set:
        sequences = set()

        def visit(value, parent_key=""):
            if isinstance(value, dict):
                for key, item in value.items():
                    visit(item, key)
                return
            if isinstance(value, list):
                for item in value:
                    visit(item, parent_key)
                return
            if "sequence" not in str(parent_key).lower():
                return
            try:
                sequences.add(int(value))
            except (TypeError, ValueError):
                return

        visit(rule_candidate or {})
        return sequences


    @staticmethod
    def _copy_limited_message(message: dict) -> dict:
        copied = dict(message)
        content = copied.get("content") or ""
        if len(content) > STRATEGY_CONTEXT_MAX_MESSAGE_CHARS:
            copied["content"] = content[:STRATEGY_CONTEXT_MAX_MESSAGE_CHARS]
            copied["content_truncated"] = True
        else:
            copied["content_truncated"] = False
        return copied


    @staticmethod
    def _apply_strategy_context_limits(messages: list, rule_evidence_sequences: set) -> tuple:
        limited = [ContextBuilder._copy_limited_message(msg) for msg in (messages or [])]
        total_chars = sum(len(msg.get("content") or "") for msg in limited)
        over_limit = (
            len(limited) > STRATEGY_CONTEXT_MAX_MESSAGES
            or total_chars > STRATEGY_CONTEXT_MAX_CHARS
        )
        if not over_limit:
            return limited, False, []

        keep_sequences = {
            msg.get("sequence") for msg in limited[:5]
            if msg.get("sequence") is not None
        }
        keep_sequences.update(
            msg.get("sequence") for msg in limited[-20:]
            if msg.get("sequence") is not None
        )
        keep_sequences.update(
            seq for seq in (rule_evidence_sequences or set())
            if seq is not None
        )
        kept = [
            msg for msg in limited
            if msg.get("sequence") in keep_sequences
        ]
        kept.sort(key=lambda item: (item.get("sequence") is None, item.get("sequence") or 0, item.get("id") or 0))
        omitted = [
            msg.get("sequence") for msg in limited
            if msg.get("sequence") not in keep_sequences and msg.get("sequence") is not None
        ]
        return kept, True, ContextBuilder._sequence_ranges(omitted)


    @staticmethod
    def _sequence_ranges(sequences: list) -> list:
        values = sorted({int(seq) for seq in sequences if seq is not None})
        if not values:
            return []
        ranges = []
        start = prev = values[0]
        for seq in values[1:]:
            if seq == prev + 1:
                prev = seq
                continue
            ranges.append([start, prev])
            start = prev = seq
        ranges.append([start, prev])
        return ranges


    @staticmethod
    def _get_document_summary(group_id: int, session_context: dict) -> dict:
        session = (session_context or {}).get("session") or {}
        task = (session_context or {}).get("task") or {}
        session_no = session.get("session_no")
        task_id = task.get("task_id")
        if task_id is not None and session_no is not None:
            row = query_one(
                """
                SELECT id, status, state_revision, content_text, updated_at, submitted_at
                FROM collaborative_documents
                WHERE group_id=? AND task_id=? AND session_no=?
                ORDER BY id DESC LIMIT 1
                """,
                (group_id, task_id, session_no),
            )
        else:
            row = query_one(
                """
                SELECT id, status, state_revision, content_text, updated_at, submitted_at
                FROM collaborative_documents
                WHERE group_id=?
                ORDER BY id DESC LIMIT 1
                """,
                (group_id,),
            )
        if not row:
            return {
                "exists": False,
                "status": None,
                "state_revision": None,
                "summary": None,
                "updated_at": None,
                "submitted_at": None,
            }
        text = (row["content_text"] or "").strip()
        return {
            "exists": True,
            "status": row["status"],
            "state_revision": row["state_revision"],
            "summary": text[:800] if text else None,
            "summary_truncated": len(text) > 800,
            "updated_at": row["updated_at"],
            "submitted_at": row["submitted_at"],
        }


    @staticmethod
    def _get_strategy_runtime_context(
        group_id: int,
        session_context: dict,
        messages: list,
        *,
        trigger_source: str = "auto_state",
        state_code: str = None,
        monitor_run_id: int = None,
        cutoff_sequence: int = None,
    ) -> dict:
        session_sql, session_params = ContextBuilder._message_session_filter(
            "m",
            session_context,
        )
        member_count = query_one(
            """
            SELECT COUNT(*) AS c
            FROM group_members gm
            JOIN users u ON u.id = gm.user_id
            WHERE gm.group_id=? AND u.role='student'
            """,
            (group_id,),
        )
        last_student = query_one(
            f"""
            SELECT m.created_at
            FROM messages m
            WHERE m.group_id=?
              AND {session_sql}
              AND COALESCE(m.role, '')='student'
            ORDER BY m.sequence DESC, m.id DESC
            LIMIT 1
            """,
            (group_id, *session_params),
        )
        seconds_since = None
        try:
            if last_student and last_student["created_at"]:
                seconds_since = int((datetime.now() - parse_dt(last_student["created_at"])).total_seconds())
        except Exception:
            seconds_since = None
        room = query_one(
            "SELECT state, lock_token, active_intervention_run_id, last_intervention_at FROM groups WHERE id=?",
            (group_id,),
        )
        runtime = {
            "online_student_count": int(member_count["c"] or 0) if member_count else 0,
            "seconds_since_last_student_message": seconds_since,
            "message_count_in_context": len(messages or []),
            "server_time": now_str(),
            "trigger_source": trigger_source or "auto_state",
            "help_request": ContextBuilder._get_help_request_runtime(group_id, session_context),
            "cooldown": ContextBuilder._get_cooldown_runtime(
                group_id,
                session_context,
            ),
            "room": {
                "state": room["state"] if room else None,
                "locked": bool(room and (room["lock_token"] or room["active_intervention_run_id"])),
                "active_intervention_run_id": room["active_intervention_run_id"] if room else None,
                "last_intervention_at": room["last_intervention_at"] if room else None,
            },
        }
        if state_code == "negative_silence":
            runtime["silence"] = ContextBuilder._get_silence_runtime_context(
                group_id,
                session_context,
                monitor_run_id=monitor_run_id,
                cutoff_sequence=cutoff_sequence,
                fallback_silent_seconds=seconds_since,
            )
        return runtime

    @staticmethod
    def _get_silence_runtime_context(
        group_id: int,
        session_context: dict,
        *,
        monitor_run_id: int = None,
        cutoff_sequence: int = None,
        fallback_silent_seconds: int = None,
    ) -> dict:
        session = (session_context or {}).get("session") or {}
        params = [group_id]
        where = [
            "group_id=?",
            "state_code='negative_silence'",
            "source='silence_rule'",
        ]
        if session.get("session_id") is not None:
            where.append("session_id=?")
            params.append(session.get("session_id"))
        if monitor_run_id is not None or cutoff_sequence is not None:
            where.append(
                """
                (
                    (? IS NOT NULL AND source_run_id=?)
                    OR (? IS NOT NULL AND trigger_sequence=?)
                )
                """
            )
            params.extend(
                [
                    monitor_run_id,
                    monitor_run_id,
                    cutoff_sequence,
                    cutoff_sequence,
                ]
            )
        row = query_one(
            f"""
            SELECT id, silence_event_key, dedupe_key, trigger_sequence,
                   raw_silence_started_at, threshold_reached_at, detected_at,
                   last_observed_at, silent_seconds_at_detection, is_active,
                   intervention_scheduled_at, intervention_run_id,
                   intervention_published_at, intervention_disposition
            FROM collaboration_state_segments
            WHERE {' AND '.join(where)}
            ORDER BY is_active DESC, id DESC
            LIMIT 1
            """,
            tuple(params),
        )
        latest_student = query_one(
            """
            SELECT sequence, created_at
            FROM messages
            WHERE group_id=? AND sequence IS NOT NULL
              AND COALESCE(role, '')='student'
              AND (? IS NULL OR session_id=?)
            ORDER BY sequence DESC, id DESC
            LIMIT 1
            """,
            (
                group_id,
                session.get("session_id"),
                session.get("session_id"),
            ),
        )
        silent_seconds = fallback_silent_seconds
        if row and row["raw_silence_started_at"]:
            try:
                silent_seconds = max(
                    int(
                        (
                            datetime.now()
                            - parse_dt(row["raw_silence_started_at"])
                        ).total_seconds()
                    ),
                    int(row["silent_seconds_at_detection"] or 0),
                )
            except Exception:
                silent_seconds = (
                    int(row["silent_seconds_at_detection"] or 0)
                    or fallback_silent_seconds
                )
        trigger_sequence = (
            int(row["trigger_sequence"])
            if row and row["trigger_sequence"] is not None
            else cutoff_sequence
        )
        latest_student_sequence = (
            int(latest_student["sequence"])
            if latest_student and latest_student["sequence"] is not None
            else None
        )
        return {
            "segment_id": int(row["id"]) if row else None,
            "silence_event_key": (
                (row["silence_event_key"] or row["dedupe_key"]) if row else None
            ),
            "trigger_sequence": trigger_sequence,
            "latest_student_sequence": latest_student_sequence,
            "last_student_message_at": (
                latest_student["created_at"] if latest_student else None
            ),
            "raw_silence_started_at": (
                row["raw_silence_started_at"] if row else None
            ),
            "threshold_reached_at": (
                row["threshold_reached_at"] if row else None
            ),
            "detected_at": row["detected_at"] if row else None,
            "last_observed_at": row["last_observed_at"] if row else None,
            "silent_seconds": silent_seconds,
            "is_active": bool(row["is_active"]) if row else False,
            "student_has_resumed": (
                trigger_sequence is not None
                and latest_student_sequence is not None
                and latest_student_sequence != trigger_sequence
            ),
            "intervention_scheduled_at": (
                row["intervention_scheduled_at"] if row else None
            ),
            "intervention_run_id": (
                row["intervention_run_id"] if row else None
            ),
            "intervention_published_at": (
                row["intervention_published_at"] if row else None
            ),
            "intervention_disposition": (
                row["intervention_disposition"] if row else None
            ),
        }


    @staticmethod
    def _get_help_request_runtime(group_id: int, session_context: dict) -> dict:
        session = (session_context or {}).get("session") or {}
        params = [group_id]
        where = ["group_id=?", "UPPER(COALESCE(status, '')) IN ('QUEUED','RUNNING','PENDING','PROCESSING')"]
        if session.get("session_id") is not None:
            where.append("session_id=?")
            params.append(session.get("session_id"))
        rows = query_all(
            f"""
            SELECT id, status, created_at
            FROM help_requests
            WHERE {' AND '.join(where)}
            ORDER BY id DESC LIMIT 5
            """,
            tuple(params),
        )
        return {
            "has_pending_or_processing": bool(rows),
            "pending_count": len(rows),
            "request_ids": [row["id"] for row in rows],
            "latest_status": rows[0]["status"] if rows else None,
            "latest_created_at": rows[0]["created_at"] if rows else None,
        }


    @staticmethod
    def _get_cooldown_runtime(group_id: int, session_context: dict = None) -> dict:
        from config import INTERVENTION_V2_COOLDOWN_SECONDS

        seconds = int(INTERVENTION_V2_COOLDOWN_SECONDS or 0)
        since_dt = datetime.now() - timedelta(seconds=seconds)
        since = since_dt.strftime("%Y-%m-%d %H:%M:%S")
        session_id = (
            ((session_context or {}).get("session") or {}).get("session_id")
        )
        row = query_one(
            """
            SELECT COUNT(*) AS c, MAX(actual_published_at) AS last_published_at
            FROM intervention_runs
            WHERE group_id=? AND created_at>=?
              AND (? IS NULL OR session_id=?)
              AND COALESCE(agent_type, 'strategy')='strategy'
              AND status IN ('PUBLISHED','FALLBACK')
            """,
            (group_id, since, session_id, session_id),
        )
        recent_count = int(row["c"] or 0) if row else 0
        return {
            "cooldown_seconds": seconds,
            "active": recent_count > 0,
            "recent_published_count": recent_count,
            "last_published_at": row["last_published_at"] if row else None,
        }


    @staticmethod
    def _summarize_allowed_strategies(strategies: list) -> list:
        result = []
        for strategy in strategies or []:
            result.append({
                "id": strategy.get("id"),
                "version": strategy.get("version"),
                "applicable_states": strategy.get("applicable_states") or [],
                "goal": strategy.get("goal"),
                "generator_instruction": strategy.get("generator_instruction"),
                "max_chars": strategy.get("max_chars"),
                "cooldown_seconds": strategy.get("cooldown_seconds"),
            })
        return result



    @staticmethod

    def _get_task_info(group_id: int) -> dict:

        """获取讨论题目与要求、当前任务阶段。"""

        task = get_current_learning_task()

        if not task:

            task = query_one(

                "SELECT * FROM learning_tasks WHERE is_active=1 ORDER BY sort_order ASC, id DESC LIMIT 1"

            )

        if not task:

            task = query_one(

                "SELECT id, title, description FROM tasks WHERE is_active=1 ORDER BY id DESC LIMIT 1"

            )

        if not task:

            return {

                "topic": "无活动任务",

                "description": "",

                "question": "",

                "goal": "",

                "output_requirement": "",

                "session_no": 0,

            }



        task_data = dict(task)

        return {

            "id": task_data.get("id"),

            "topic": task_data.get("title") or "未命名任务",

            "description": task_data.get("description") or "",

            "question": task_data.get("question") or "",

            "goal": task_data.get("task_goal") or task_data.get("description") or "",

            "output_requirement": task_data.get("output_requirement") or "",

            "session_no": ContextBuilder._get_session_no(group_id),

        }



    @staticmethod

    def _get_session_no(group_id: int) -> int:

        rows = query_all(
            """
            SELECT session_no FROM (
                SELECT session_no, created_at, id FROM process_events WHERE group_id=?
                UNION ALL
                SELECT session_no, created_at, id FROM messages WHERE group_id=?
                UNION ALL
                SELECT session_no, updated_at AS created_at, id FROM experiment_sessions WHERE status='running'
            )
            WHERE session_no IS NOT NULL AND TRIM(CAST(session_no AS TEXT)) <> ''
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (group_id, group_id),
        )

        if not rows:
            return 0

        try:
            return int(rows[0]["session_no"] or 0)
        except (TypeError, ValueError):
            return 0



    @staticmethod

    def _get_room_info(group_id: int) -> dict:

        row = query_one(

            "SELECT id, name, state, version, last_message_sequence, cutoff_sequence, created_at, condition, auto_intervention_enabled FROM groups WHERE id=?",

            (group_id,),

        )

        if not row:

            return {}

        return {

            "name": row["name"],

            "state": row["state"],

            "condition": row["condition"],

            "message_count": row["last_message_sequence"],

            "auto_intervention_enabled": bool(row["auto_intervention_enabled"]),

        }



    @staticmethod

    def _get_recent_messages(group_id: int, limit: int = None) -> list:

        """获取最??N 条消息，按时间升序。"""

        if limit is None:

            limit = INTERVENTION_V2_MAX_MESSAGES_FOR_CONTEXT

        rows = query_all(

            """SELECT m.id, m.content, m.sequence, m.created_at,

                      u.real_name, u.username, u.role,

                      COALESCE(NULLIF(TRIM(u.real_name), ''), u.username, '成员') AS display_name

               FROM messages m

               JOIN users u ON m.user_id=u.id

               WHERE m.group_id=?

               ORDER BY m.id DESC LIMIT ?""",

            (group_id, limit),

        )

        messages = []

        for row in reversed(rows):

            messages.append({

                "sequence": row["sequence"],

                "speaker": row["display_name"],

                "role": row["role"],

                "content": row["content"],

                "time": row["created_at"],

            })

        return messages



    @staticmethod

    def _get_trigger_window(group_id: int, monitor_run: dict, limit: int = None) -> dict:

        """Return student messages up to the monitor cutoff for prompt grounding."""

        if limit is None:

            limit = INTERVENTION_V2_MAX_MESSAGES_FOR_CONTEXT

        monitor = dict(monitor_run) if monitor_run else {}

        cutoff = monitor.get("cutoff_sequence")

        rows = query_all(

            """SELECT m.id, m.content, m.sequence, m.created_at,

                      u.role,

                      COALESCE(NULLIF(TRIM(u.real_name), ''), u.username, '成员') AS display_name

               FROM messages m

               JOIN users u ON m.user_id=u.id

               WHERE m.group_id=?

                 AND u.role='student'

                 AND (? IS NULL OR m.sequence<=?)

               ORDER BY m.sequence DESC, m.id DESC LIMIT ?""",

            (group_id, cutoff, cutoff, limit),

        )

        messages = []

        for row in reversed(rows):

            messages.append({

                "sequence": row["sequence"],

                "speaker": row["display_name"],

                "role": row["role"],

                "content": row["content"],

                "time": row["created_at"],

            })

        first_sequence = messages[0]["sequence"] if messages else None

        last_sequence = messages[-1]["sequence"] if messages else cutoff

        return {

            "from_sequence": first_sequence,

            "to_sequence": last_sequence,

            "cutoff_sequence": cutoff,

            "message_count": len(messages),

            "messages": messages,

            "description": "从上下文窗口起点到本次触发状态截止序号的学生消息",

        }



    @staticmethod

    def _get_participation_stats(group_id: int) -> dict:

        """获取小组参与统计。"""

        rows = query_all(

            """SELECT u.real_name, u.username, COALESCE(NULLIF(TRIM(u.real_name), ''), u.username, '成员') AS display_name,

                      COUNT(m.id) AS msg_count

               FROM messages m

               JOIN users u ON m.user_id=u.id

               WHERE m.group_id=? AND u.role='student'

               GROUP BY u.id

               ORDER BY msg_count DESC""",

            (group_id,),

        )

        if not rows:

            return {"total_messages": 0, "active_members": 0, "members": []}



        members = [{"name": r["display_name"], "messages": r["msg_count"]} for r in rows]

        return {

            "total_messages": sum(r["msg_count"] for r in rows),

            "active_members": len(members),

            "members": members,

        }



    @staticmethod

    def _summarize_monitor_run(monitor_run: dict) -> dict:

        """提取 monitor_run 的关键信息。"""

        if not monitor_run:

            return {}

        if not isinstance(monitor_run, dict):

            monitor_run = dict(monitor_run)

        rule_result = None

        llm_result = None

        try:

            if monitor_run.get("rule_result_json"):

                rule_result = json.loads(monitor_run["rule_result_json"]) if isinstance(monitor_run["rule_result_json"], str) else monitor_run["rule_result_json"]

            if monitor_run.get("llm_result_json"):

                llm_result = json.loads(monitor_run["llm_result_json"]) if isinstance(monitor_run["llm_result_json"], str) else monitor_run["llm_result_json"]

        except (json.JSONDecodeError, TypeError):

            pass



        return {

            "id": monitor_run["id"],

            "cutoff_sequence": monitor_run.get("cutoff_sequence"),

            "final_state": monitor_run.get("final_state"),

            "confidence": monitor_run.get("confidence"),

            "trigger_type": monitor_run.get("trigger_type"),

            "rule_winning_state": rule_result.get("winning_state_code") if rule_result else None,

            "llm_primary_state": llm_result.get("primary_state") if llm_result else None,

            "reasoning": (rule_result.get("reason") or (llm_result.get("reason") if llm_result else "")) if rule_result or llm_result else None,

        }



    @staticmethod

    def _get_recent_interventions(group_id: int, limit: int = 3) -> list:

        """获取最近介入记录。"""

        rows = query_all(

            """SELECT id, status, strategy_id, fallback_used, generated_message,

                      detected_state, confidence, created_at, completed_at

               FROM intervention_runs

               WHERE group_id=?
                 AND COALESCE(agent_type, 'strategy')='strategy'

               ORDER BY id DESC LIMIT ?""",

            (group_id, limit),

        )

        result = []

        for row in rows:

            result.append({

                "id": row["id"],

                "status": row["status"],

                "strategy_id": row["strategy_id"],

                "fallback_used": bool(row["fallback_used"]),

                "generated_message": row["generated_message"],

                "detected_state": row["detected_state"],

                "confidence": row["confidence"],

                "created_at": row["created_at"],

                "completed_at": row["completed_at"],

            })

        return result

