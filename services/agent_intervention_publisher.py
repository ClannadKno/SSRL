# -*- coding: utf-8 -*-
"""Shared persistence for student-visible agent interventions."""

import json
import logging

from auth import get_sera_user_id
from db import begin_discussion_observation, db, now_str, record_process_event

logger = logging.getLogger(__name__)


TRIGGER_AUTO_STATE = "auto_state"
TRIGGER_SILENCE_RULE = "silence_rule"
TRIGGER_STUDENT_HELP = "student_help_request"
TRIGGER_TEACHER = "teacher"
TRIGGER_EMOTION_SCHEDULE = "emotion_schedule"
TRIGGER_LEGACY_UNKNOWN = "legacy_unknown"

VALID_TRIGGER_SOURCES = frozenset(
    {
        TRIGGER_AUTO_STATE,
        TRIGGER_SILENCE_RULE,
        TRIGGER_STUDENT_HELP,
        TRIGGER_TEACHER,
        TRIGGER_EMOTION_SCHEDULE,
        TRIGGER_LEGACY_UNKNOWN,
    }
)

TRIGGER_SOURCE_ALIASES = {
    "auto_v2": TRIGGER_AUTO_STATE,
    "auto_intervention": TRIGGER_AUTO_STATE,
    "new_message": TRIGGER_AUTO_STATE,
    "student_message": TRIGGER_AUTO_STATE,
    "sera_auto": TRIGGER_AUTO_STATE,
    "sera_auto_v2": TRIGGER_AUTO_STATE,
    "student_help": TRIGGER_STUDENT_HELP,
    "student_request": TRIGGER_STUDENT_HELP,
    "teacher_confirmed": TRIGGER_TEACHER,
    "sera_teacher_confirmed": TRIGGER_TEACHER,
    "scheduled_10min": TRIGGER_EMOTION_SCHEDULE,
}


def normalize_trigger_source(value):
    text = str(value or "").strip().lower()
    if not text:
        return TRIGGER_LEGACY_UNKNOWN
    return TRIGGER_SOURCE_ALIASES.get(text, text)


def _default_push_mode(trigger_source, teacher_user_id=None):
    if teacher_user_id or trigger_source == TRIGGER_TEACHER:
        return "sera_teacher_confirmed"
    if trigger_source == TRIGGER_STUDENT_HELP:
        return "student_request"
    if trigger_source == TRIGGER_EMOTION_SCHEDULE:
        return "emotion_schedule"
    return "sera_auto_v2"


def _row_to_dict(row):
    return dict(row) if row else None


def _json_for_db(value):
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _resolve_cutoff_sequence(conn, group_id, source_message_id=None, cutoff_sequence=None):
    if cutoff_sequence is not None:
        return cutoff_sequence
    if source_message_id:
        row = conn.execute(
            "SELECT sequence FROM messages WHERE id=? AND group_id=?",
            (source_message_id, group_id),
        ).fetchone()
        if row and row["sequence"] is not None:
            return row["sequence"]
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) AS seq FROM messages WHERE group_id=?",
        (group_id,),
    ).fetchone()
    return row["seq"] if row else 0


def _next_message_sequence(conn, group_id):
    conn.execute(
        """
        UPDATE groups
           SET last_message_sequence =
               MAX(
                   COALESCE(last_message_sequence, 0),
                   COALESCE((SELECT MAX(sequence) FROM messages WHERE group_id=?), 0)
               ) + 1
         WHERE id=?
        """,
        (group_id, group_id),
    )
    row = conn.execute(
        "SELECT last_message_sequence FROM groups WHERE id=?",
        (group_id,),
    ).fetchone()
    return row["last_message_sequence"] if row else None


def _existing_help_publish(conn, help_request_id):
    if not help_request_id:
        return None
    row = conn.execute(
        """
        SELECT hr.intervention_run_id, hr.response_message_id,
               ir.message_id AS run_message_id
          FROM help_requests hr
          LEFT JOIN intervention_runs ir ON ir.id=hr.intervention_run_id
         WHERE hr.id=?
        """,
        (help_request_id,),
    ).fetchone()
    if not row:
        return None
    message_id = row["response_message_id"] or row["run_message_id"]
    if row["intervention_run_id"] or message_id:
        return {
            "ok": True,
            "duplicate": True,
            "intervention_run_id": row["intervention_run_id"],
            "message_id": message_id,
        }
    return None


def publish_agent_intervention(
    *,
    group_id,
    message,
    trigger_source,
    agent_type="strategy",
    intervention_run_id=None,
    monitor_run_id=None,
    state_assessment_id=None,
    help_request_id=None,
    source_student_message_id=None,
    session_id=None,
    discussion_id=None,
    task_id=None,
    session_no=None,
    cutoff_sequence=None,
    strategy_id=None,
    title=None,
    teacher_reason=None,
    prompt_version=None,
    fallback_used=False,
    condition=None,
    push_mode=None,
    teacher_user_id=None,
    suggestion_id=None,
    decision_id=None,
    template_id=None,
    sub_category=None,
    strategy_type=None,
    strategy_version=None,
    strategy_pool_json=None,
    selected_strategy=None,
    strategy_source=None,
    model_name=None,
    detected_state=None,
    confidence=None,
    lock_token=None,
    help_request_status=None,
    help_intent=None,
    assessment_batch_id=None,
    target_segment_id=None,
    reason_code=None,
    active_segment_index=None,
    evidence_sequences=None,
    guard_result=None,
    guard_reason=None,
    retry_count=0,
    raw_response=None,
    metadata=None,
    strategy_pipeline_run_id=None,
    canonical_sub_state_code=None,
    strategy_candidate_ids_json=None,
    strategy_selection_reason=None,
    evidence_message_ids_json=None,
    input_cutoff_student_sequence=None,
    generated_text=None,
    validated_text=None,
    publish_status=None,
    expected_latest_student_sequence=None,
    expected_lock_owner_run_id=None,
):
    """Publish one student-visible intervention with a unified audit chain."""
    trigger_source = normalize_trigger_source(trigger_source)
    if trigger_source not in VALID_TRIGGER_SOURCES:
        return {"ok": False, "reason": "invalid_trigger_source", "trigger_source": trigger_source}
    if not message:
        return {"ok": False, "reason": "empty_message", "trigger_source": trigger_source}

    sera_user_id = get_sera_user_id()
    if not sera_user_id:
        return {"ok": False, "reason": "sera_user_not_found", "trigger_source": trigger_source}

    agent_type = (agent_type or "strategy").strip().lower() or "strategy"
    push_mode = push_mode or _default_push_mode(trigger_source, teacher_user_id=teacher_user_id)
    now = now_str()
    conn = db()
    try:
        conn.execute("BEGIN")
        group_row = conn.execute(
            """
            SELECT id, condition, state, lock_token, lock_expires_at,
                   active_intervention_run_id
              FROM groups
             WHERE id=?
            """,
            (group_id,),
        ).fetchone()
        if not group_row:
            conn.rollback()
            return {"ok": False, "reason": "group_not_found", "group_id": group_id}

        if lock_token and group_row["lock_token"] != lock_token:
            conn.rollback()
            return {"ok": False, "reason": "lock_token_mismatch", "group_id": group_id}
        if lock_token:
            if group_row["state"] != "AI_INTERVENING":
                conn.rollback()
                return {"ok": False, "reason": "room_not_locked", "group_id": group_id}
            if (
                group_row["lock_expires_at"]
                and group_row["lock_expires_at"] <= now
            ):
                conn.rollback()
                return {
                    "ok": False,
                    "reason": "lock_expired",
                    "group_id": group_id,
                    "lock_expires_at": group_row["lock_expires_at"],
                }
        if lock_token and expected_lock_owner_run_id is not None:
            if group_row["active_intervention_run_id"] != expected_lock_owner_run_id:
                conn.rollback()
                return {"ok": False, "reason": "lock_owner_mismatch", "group_id": group_id}

        if discussion_id is not None:
            discussion = conn.execute(
                """
                SELECT gsd.status AS discussion_status, es.status AS session_status
                FROM group_session_discussions AS gsd
                JOIN experiment_sessions AS es ON es.id=gsd.session_id
                WHERE gsd.id=? AND gsd.group_id=? AND gsd.session_id=?
                """,
                (discussion_id, group_id, session_id),
            ).fetchone()
            if (
                not discussion
                or discussion["discussion_status"] != "running"
                or discussion["session_status"] != "running"
            ):
                conn.rollback()
                return {
                    "ok": False,
                    "reason": "discussion_not_running",
                    "discussion_id": discussion_id,
                }

        if target_segment_id is not None:
            target = conn.execute(
                """
                SELECT s.id, s.assessment_batch_id, s.group_id, s.session_id,
                       s.source, s.assessment_status, s.state_code,
                       b.status AS batch_status, b.discussion_id
                FROM collaboration_state_segments AS s
                JOIN state_assessment_batches AS b ON b.id=s.assessment_batch_id
                WHERE s.id=?
                """,
                (target_segment_id,),
            ).fetchone()
            if (
                not target
                or target["assessment_batch_id"] != assessment_batch_id
                or target["group_id"] != group_id
                or target["session_id"] != session_id
                or target["discussion_id"] != discussion_id
                or target["source"] != "llm"
                or target["assessment_status"] != "confirmed"
                or target["state_code"] not in {
                    "conflict_tension",
                    "negative_silence",
                    "frustration_stuck",
                    "task_detached",
                }
                or target["batch_status"] != "succeeded"
            ):
                conn.rollback()
                return {
                    "ok": False,
                    "reason": "target_segment_scope_mismatch",
                    "target_segment_id": target_segment_id,
                }

        help_row = None
        if help_request_id:
            existing = _existing_help_publish(conn, help_request_id)
            if existing:
                conn.rollback()
                return existing
            help_row = conn.execute(
                "SELECT * FROM help_requests WHERE id=?",
                (help_request_id,),
            ).fetchone()
            if not help_row:
                conn.rollback()
                return {"ok": False, "reason": "help_request_not_found", "help_request_id": help_request_id}
            if int(help_row["group_id"]) != int(group_id):
                conn.rollback()
                return {"ok": False, "reason": "help_request_group_mismatch", "help_request_id": help_request_id}
            session_id = session_id if session_id is not None else help_row["session_id"]
            task_id = task_id if task_id is not None else help_row["task_id"]
            session_no = session_no if session_no is not None else help_row["session_no"]
            discussion_id = (
                discussion_id
                if discussion_id is not None
                else help_row["discussion_id"]
            )
            source_student_message_id = source_student_message_id or help_row["source_message_id"]

        from services.discussion_scope import resolve_discussion_scope

        scope = resolve_discussion_scope(
            conn,
            group_id=group_id,
            message_id=source_student_message_id,
            sequence=cutoff_sequence,
            session_id=session_id,
            session_no=session_no,
            task_id=task_id,
            discussion_id=discussion_id,
            allow_legacy_fallback=False,
        )
        session_id = scope.session_id
        session_no = scope.session_no
        task_id = scope.task_id
        discussion_id = scope.discussion_id

        if session_id is not None:
            session = conn.execute(
                "SELECT id FROM experiment_sessions WHERE id=?",
                (session_id,),
            ).fetchone()
            if not session:
                conn.rollback()
                return {"ok": False, "reason": "session_not_found", "session_id": session_id}
        if task_id is not None:
            task = conn.execute(
                "SELECT id FROM learning_tasks WHERE id=?",
                (task_id,),
            ).fetchone()
            if not task:
                conn.rollback()
                return {"ok": False, "reason": "task_not_found", "task_id": task_id}

        cutoff_sequence = _resolve_cutoff_sequence(
            conn,
            group_id,
            source_message_id=source_student_message_id,
            cutoff_sequence=cutoff_sequence,
        )
        if expected_latest_student_sequence is not None:
            latest = conn.execute(
                """
                SELECT MAX(m.sequence) AS latest_sequence
                  FROM messages AS m
                  LEFT JOIN users AS u ON u.id=m.user_id
                 WHERE m.group_id=?
                   AND COALESCE(m.session_id, 0)=COALESCE(?, 0)
                   AND COALESCE(m.discussion_id, 0)=COALESCE(?, 0)
                   AND COALESCE(NULLIF(TRIM(m.role), ''), m.sender_type, u.role)='student'
                   AND m.sequence IS NOT NULL
                """,
                (group_id, session_id, discussion_id),
            ).fetchone()
            latest_sequence = (
                int(latest["latest_sequence"])
                if latest and latest["latest_sequence"] is not None
                else None
            )
            if latest_sequence is not None and latest_sequence > int(expected_latest_student_sequence):
                conn.rollback()
                return {
                    "ok": False,
                    "reason": "stale_student_sequence",
                    "latest_student_sequence": latest_sequence,
                    "expected_latest_student_sequence": int(expected_latest_student_sequence),
                }
        run = None
        if intervention_run_id:
            run = conn.execute(
                "SELECT * FROM intervention_runs WHERE id=? AND group_id=?",
                (intervention_run_id, group_id),
            ).fetchone()
            if not run:
                conn.rollback()
                return {"ok": False, "reason": "intervention_run_not_found", "intervention_run_id": intervention_run_id}
            if run["message_id"]:
                message_id = run["message_id"]
                if lock_token:
                    release_cur = conn.execute(
                        """
                        UPDATE groups
                           SET state='OPEN', version=version+1,
                               lock_token=NULL, lock_expires_at=NULL,
                               active_intervention_run_id=NULL,
                               last_intervention_at=COALESCE(last_intervention_at, ?)
                         WHERE id=? AND lock_token=?
                        """,
                        (now, group_id, lock_token),
                    )
                    if release_cur.rowcount != 1:
                        raise RuntimeError("room_lease_release_failed")
                conn.execute(
                    """
                    UPDATE intervention_runs
                       SET status=CASE
                             WHEN COALESCE(status, '') IN ('PUBLISHED','FALLBACK') THEN status
                             ELSE 'PUBLISHED'
                           END,
                           decision=COALESCE(decision, 'INTERVENE'),
                           published_at=COALESCE(published_at, ?),
                           actual_published_at=COALESCE(actual_published_at, ?),
                           completed_at=COALESCE(completed_at, ?)
                     WHERE id=?
                    """,
                    (now, now, now, intervention_run_id),
                )
                if run["target_segment_id"]:
                    conn.execute(
                        """
                        UPDATE collaboration_state_segments
                        SET intervention_run_id=?,
                            intervention_published_at=COALESCE(
                                intervention_published_at, ?
                            ),
                            intervention_disposition='PUBLISHED',
                            updated_at=?
                        WHERE id=? AND state_code='negative_silence'
                          AND source='silence_rule'
                        """,
                        (
                            intervention_run_id,
                            now,
                            now,
                            run["target_segment_id"],
                        ),
                    )
                conn.commit()
                return {
                    "ok": True,
                    "duplicate": True,
                    "intervention_run_id": intervention_run_id,
                    "message_id": message_id,
                    "status": "PUBLISHED",
                }
            monitor_run_id = monitor_run_id if monitor_run_id is not None else run["monitor_run_id"]
            state_assessment_id = state_assessment_id if state_assessment_id is not None else run["state_assessment_id"]
            session_id = session_id if session_id is not None else run["session_id"]
            discussion_id = discussion_id if discussion_id is not None else run["discussion_id"]
            session_no = session_no if session_no is not None else run["session_no"]
            task_id = task_id if task_id is not None else run["task_id"]
            help_request_id = help_request_id if help_request_id is not None else run["help_request_id"]
        else:
            metadata_payload = {
                "trigger_source": trigger_source,
                "help_request_id": help_request_id,
                "source_student_message_id": source_student_message_id,
                "state_assessment_id": state_assessment_id,
                "monitor_run_id": monitor_run_id,
                "assessment_batch_id": assessment_batch_id,
                "target_segment_id": target_segment_id,
                "discussion_id": discussion_id,
                **(metadata or {}),
            }
            cur = conn.execute(
                """
                INSERT INTO intervention_runs(
                    group_id, monitor_run_id, state_assessment_id, session_id,
                    session_no, discussion_id, task_id,
                    cutoff_sequence, status, decision, detected_state, confidence,
                    generated_message, message_id, prompt_version, fallback_used,
                    strategy_id, selected_strategy_id, model_profile, lock_token,
                    lock_acquired, created_at, completed_at, agent_type, trigger_type,
                    dry_run, metadata_json, help_request_id, assessment_batch_id,
                    target_segment_id, reason_code, active_segment_index,
                    evidence_sequences_json, guard_result, guard_reason,
                    retry_count, raw_response, started_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    group_id,
                    monitor_run_id,
                    state_assessment_id,
                    session_id,
                    session_no,
                    discussion_id,
                    task_id,
                    cutoff_sequence,
                    "PENDING",
                    None,
                    detected_state,
                    confidence,
                    None,
                    None,
                    prompt_version,
                    1 if fallback_used else 0,
                    strategy_id,
                    strategy_id,
                    model_name,
                    lock_token,
                    1 if lock_token else 0,
                    now,
                    None,
                    agent_type,
                    trigger_source,
                    0,
                    _json_for_db(metadata_payload),
                    help_request_id,
                    assessment_batch_id,
                    target_segment_id,
                    reason_code,
                    active_segment_index,
                    _json_for_db(evidence_sequences),
                    guard_result,
                    guard_reason,
                    int(retry_count or 0),
                    raw_response,
                    now,
                ),
            )
            intervention_run_id = cur.lastrowid

        help_coverage = {}
        if help_request_id:
            from services.help_request_coverage_service import (
                HelpRequestCoverageService,
            )

            help_coverage = HelpRequestCoverageService.resolve_handled_issue(
                conn,
                group_id=group_id,
                session_id=session_id,
                source_message_id=source_student_message_id,
                monitor_run_id=monitor_run_id,
                state_assessment_id=state_assessment_id,
                detected_state=detected_state,
                target_segment_id=target_segment_id,
                evidence_sequences=evidence_sequences,
                cutoff_sequence=cutoff_sequence,
            )

        client_message_id = (
            f"agent-help-{help_request_id}"
            if help_request_id
            else f"agent-{agent_type}-{intervention_run_id}"
        )
        existing_msg = conn.execute(
            """
            SELECT id FROM messages
             WHERE group_id=? AND user_id=? AND client_message_id=?
             ORDER BY id DESC LIMIT 1
            """,
            (group_id, sera_user_id, client_message_id),
        ).fetchone()
        new_message_created = False
        if existing_msg:
            message_id = existing_msg["id"]
        else:
            sequence = _next_message_sequence(conn, group_id)
            msg_metadata = {
                "trigger_source": trigger_source,
                "help_request_id": help_request_id,
                "source_student_message_id": source_student_message_id,
                "state_assessment_id": state_assessment_id,
                "monitor_run_id": monitor_run_id,
                "assessment_batch_id": assessment_batch_id,
                "target_segment_id": target_segment_id,
                "discussion_id": discussion_id,
                "strategy_pipeline_run_id": strategy_pipeline_run_id,
                "canonical_sub_state_code": canonical_sub_state_code,
                "input_cutoff_student_sequence": input_cutoff_student_sequence,
            }
            conn.execute(
                """
                INSERT INTO messages(
                    group_id, user_id, content, role, sender_type,
                    client_message_id, intervention_run_id, sequence, created_at,
                    session_no, task_id, session_id, strategy_id, metadata_json,
                    agent_type, trigger_source, discussion_id,
                    scope_resolved_from, legacy_scope_fallback, scope_fallback_reason
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    group_id,
                    sera_user_id,
                    message,
                    "agent",
                    "agent",
                    client_message_id,
                    intervention_run_id,
                    sequence,
                    now,
                    session_no,
                    task_id,
                    session_id,
                    strategy_id,
                    _json_for_db(msg_metadata),
                    agent_type,
                    trigger_source,
                    discussion_id,
                    scope.resolved_from,
                    1 if scope.is_legacy_fallback else 0,
                    scope.fallback_reason,
                ),
            )
            message_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            new_message_created = True

        run_status = "FALLBACK" if fallback_used else "PUBLISHED"
        conn.execute(
            """
            UPDATE intervention_runs
               SET status=?,
                   decision='INTERVENE',
                   generated_message=?,
                   selected_strategy_id=?,
                   strategy_id=?,
                   selected_strategy=COALESCE(?, selected_strategy),
                   sub_category=COALESCE(?, sub_category),
                   strategy_pool_json=COALESCE(?, strategy_pool_json),
                   strategy_source=COALESCE(?, strategy_source),
                   teacher_reason=?,
                   message_id=?,
                   prompt_version=?,
                   generated_at=COALESCE(generated_at, ?),
                   published_at=?,
                   actual_published_at=?,
                   completed_at=?,
                   lock_token=COALESCE(?, lock_token),
                   fallback_used=?,
                   session_id=COALESCE(?, session_id),
                   task_id=COALESCE(?, task_id),
                   state_assessment_id=COALESCE(?, state_assessment_id),
                   monitor_run_id=COALESCE(?, monitor_run_id),
                   help_request_id=COALESCE(?, help_request_id),
                   assessment_batch_id=COALESCE(?, assessment_batch_id),
                   target_segment_id=COALESCE(?, target_segment_id),
                   discussion_id=COALESCE(?, discussion_id),
                   reason_code=COALESCE(?, reason_code),
                   active_segment_index=COALESCE(?, active_segment_index),
                   evidence_sequences_json=COALESCE(?, evidence_sequences_json),
                   guard_result=COALESCE(?, guard_result),
                   guard_reason=COALESCE(?, guard_reason),
                   retry_count=MAX(COALESCE(retry_count, 0), ?),
                   raw_response=COALESCE(?, raw_response),
                   started_at=COALESCE(started_at, ?),
                   agent_type=?,
                   trigger_type=?,
                   strategy_pipeline_run_id=COALESCE(?, strategy_pipeline_run_id),
                   canonical_sub_state_code=COALESCE(?, canonical_sub_state_code),
                   strategy_candidate_ids_json=COALESCE(?, strategy_candidate_ids_json),
                   strategy_selection_reason=COALESCE(?, strategy_selection_reason),
                   evidence_message_ids_json=COALESCE(?, evidence_message_ids_json),
                   input_cutoff_student_sequence=COALESCE(?, input_cutoff_student_sequence),
                   generated_text=COALESCE(?, generated_text),
                   validated_text=COALESCE(?, validated_text),
                   publish_status=COALESCE(?, publish_status)
             WHERE id=?
            """,
            (
                run_status,
                message,
                strategy_id,
                strategy_id,
                _json_for_db(selected_strategy),
                sub_category,
                strategy_pool_json,
                strategy_source,
                teacher_reason,
                message_id,
                prompt_version,
                now,
                now,
                now,
                now,
                lock_token,
                1 if fallback_used else 0,
                session_id,
                task_id,
                state_assessment_id,
                monitor_run_id,
                help_request_id,
                assessment_batch_id,
                target_segment_id,
                discussion_id,
                reason_code,
                active_segment_index,
                _json_for_db(evidence_sequences),
                guard_result,
                guard_reason,
                int(retry_count or 0),
                raw_response,
                now,
                agent_type,
                trigger_source,
                strategy_pipeline_run_id,
                canonical_sub_state_code,
                strategy_candidate_ids_json,
                strategy_selection_reason,
                evidence_message_ids_json,
                input_cutoff_student_sequence,
                generated_text,
                validated_text,
                publish_status,
                intervention_run_id,
            ),
        )
        if run and run["target_segment_id"]:
            conn.execute(
                """
                UPDATE collaboration_state_segments
                SET intervention_run_id=?,
                    intervention_published_at=?,
                    intervention_disposition=?,
                    updated_at=?
                WHERE id=? AND state_code='negative_silence'
                  AND source='silence_rule'
                """,
                (
                    intervention_run_id,
                    now,
                    run_status,
                    now,
                    run["target_segment_id"],
                ),
            )

        intervention_index_row = conn.execute(
            "SELECT COUNT(*) AS c FROM intervention_logs WHERE group_id=?",
            (group_id,),
        ).fetchone()
        intervention_index = int(intervention_index_row["c"] or 0) + 1
        log_cur = conn.execute(
            """
            INSERT INTO intervention_logs(
                group_id, intervention_id, pushed_by_user_id, push_mode,
                title, message, suggestion_id, decision_id, condition,
                trigger_source, strategy_id, template_id, sub_category,
                strategy_type, strategy_version, model_name, prompt_version,
                session_id, session_no, task_id, discussion_id,
                intervention_index, message_id,
                help_request_id, state_assessment_id, monitor_run_id,
                intervention_run_id, agent_type, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                group_id,
                intervention_run_id,
                teacher_user_id,
                push_mode,
                title or strategy_id or trigger_source,
                message,
                suggestion_id,
                decision_id,
                condition if condition is not None else group_row["condition"],
                trigger_source,
                strategy_id,
                template_id,
                sub_category,
                strategy_type,
                strategy_version,
                model_name,
                prompt_version,
                session_id,
                session_no,
                task_id,
                discussion_id,
                intervention_index,
                message_id,
                help_request_id,
                state_assessment_id,
                monitor_run_id,
                intervention_run_id,
                agent_type,
                now,
            ),
        )
        log_id = log_cur.lastrowid

        conn.execute(
            "UPDATE messages SET linked_log_id=? WHERE id=?",
            (log_id, message_id),
        )

        if suggestion_id:
            next_status = "auto_pushed" if push_mode in ("sera_auto", "sera_auto_v2") else "pushed"
            conn.execute(
                """
                UPDATE agent_suggestions
                   SET status=?, decided_at=?, decided_by_user_id=?, decision_note=?
                 WHERE id=?
                """,
                (
                    next_status,
                    now,
                    teacher_user_id,
                    f"{next_status}_run_id={intervention_run_id};log_id={log_id};message_id={message_id}",
                    suggestion_id,
                ),
            )

        if help_request_id:
            final_help_status = help_request_status or ("COMPLETED_WITH_FALLBACK" if fallback_used else "COMPLETED")
            conn.execute(
                """
                UPDATE help_requests
                   SET status=?,
                       response_message=?,
                       response_message_id=?,
                       intervention_run_id=?,
                       fallback_used=?,
                        intent=COALESCE(?, intent),
                        help_request_message_sequence=COALESCE(
                            help_request_message_sequence,
                            (SELECT sequence FROM messages WHERE id=help_requests.source_message_id)
                        ),
                        handling_status='handled',
                        handled_at=?,
                        covered_until_sequence=COALESCE(
                            ?, covered_until_sequence, help_request_message_sequence,
                            (SELECT sequence FROM messages WHERE id=help_requests.source_message_id)
                        ),
                        handled_state_code=COALESCE(?, handled_state_code),
                        handled_segment_id=COALESCE(?, handled_segment_id),
                        handled_evidence_start_sequence=COALESCE(
                            ?, handled_evidence_start_sequence
                        ),
                        handled_evidence_end_sequence=COALESCE(
                            ?, handled_evidence_end_sequence
                        ),
                        completed_at=?
                 WHERE id=?
                """,
                (
                    final_help_status,
                    message,
                    message_id,
                    intervention_run_id,
                    1 if fallback_used else 0,
                    help_intent,
                    now,
                    cutoff_sequence,
                    help_coverage.get("handled_state_code"),
                    help_coverage.get("handled_segment_id"),
                    help_coverage.get("handled_evidence_start_sequence"),
                    help_coverage.get("handled_evidence_end_sequence"),
                    now,
                    help_request_id,
                ),
            )

        if lock_token:
            try:
                from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService

                release_cur = conn.execute(
                    """
                    UPDATE groups
                       SET state=?, version=version+1,
                           lock_token=NULL, lock_expires_at=NULL,
                           active_intervention_run_id=NULL,
                           last_intervention_at=?
                     WHERE id=? AND lock_token=?
                    """,
                    (RoomLeaseService.OPEN_STATE, now, group_id, lock_token),
                )
                if release_cur.rowcount != 1:
                    raise RuntimeError("room_lease_release_failed")
            except Exception:
                raise
        else:
            conn.execute(
                "UPDATE groups SET last_intervention_at=? WHERE id=?",
                (now, group_id),
            )

        if new_message_created and agent_type == "strategy" and not teacher_user_id:
            begin_discussion_observation(
                conn,
                group_id=group_id,
                session_id=session_id,
                intervention_sequence=sequence,
                updated_at=now,
            )

        conn.commit()
    except Exception as exc:
        conn.rollback()
        logger.exception("publish_agent_intervention failed")
        return {"ok": False, "reason": "publish_failed", "error": str(exc)}
    finally:
        conn.close()

    try:
        record_process_event(
            "intervention_published",
            source="teacher" if teacher_user_id else "agent",
            group_id=group_id,
            user_id=teacher_user_id,
            session_no=session_no,
            task_id=task_id,
            related_table="intervention_runs",
            related_id=intervention_run_id,
            event_key=f"agent_intervention:{intervention_run_id}",
            payload={
                "trigger_source": trigger_source,
                "agent_type": agent_type,
                "message_id": message_id,
                "intervention_log_id": log_id,
                "help_request_id": help_request_id,
                "state_assessment_id": state_assessment_id,
                "monitor_run_id": monitor_run_id,
                "assessment_batch_id": assessment_batch_id,
                "target_segment_id": target_segment_id,
                "discussion_id": discussion_id,
                "strategy_id": strategy_id,
                "fallback_used": bool(fallback_used),
            },
            created_at=now,
        )
    except Exception as exc:
        logger.warning("Failed to record publish process event for run %s: %s", intervention_run_id, exc)

    return {
        "ok": True,
        "message_id": message_id,
        "intervention_run_id": intervention_run_id,
        "intervention_log_id": log_id,
        "trigger_source": trigger_source,
        "status": run_status,
    }
