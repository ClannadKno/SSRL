# -*- coding: utf-8 -*-
"""Coordination helpers for overlapping three-stage interventions.

The priority scale is deterministic: lower numbers win.  These helpers only
mark active audit rows terminal and release their own room leases; they never
delete queued Huey work or historical data.
"""

from __future__ import annotations

from typing import Any, Optional

from db import db, now_str
from services.three_stage_latency import record_latency_event


# Deprecated compatibility export. Mutually-exclusive Agent modes no longer
# use a cross-channel publication interval.
AGENT_CROSS_CHANNEL_MIN_INTERVAL_SECONDS = 0

ACTIVE_FINAL_STATUSES = {
    "PENDING",
    "ASSESSING",
    "WAITING_FOR_LOCK",
    "LOCKED",
    "PENDING_STAGE2",
    "PENDING_STAGE3",
    "GENERATING",
    "VALIDATING",
    "PENDING_DECISION_GATE",
    "READY_TO_PUBLISH",
}
TERMINAL_FINAL_STATUSES = {
    "PUBLISHED",
    "SUPPRESSED",
    "STALE",
    "FAILED",
    "CANCELLED",
    "SUPERSEDED",
    "SKIPPED",
}

HELP_TRIGGER_SOURCES = {"student_help", "student_help_request", "help_request"}
SESSION_END_TRIGGER_SOURCES = {
    "session_end",
    "session_timeout",
    "discussion_timeout",
    "teacher_freeze",
    "submission",
}
HIGH_RISK_SUB_STATES = {"interpersonal_conflict", "psychological_safety_risk"}

PRIORITY_BY_TRIGGER = {
    "session_end": 0,
    "session_timeout": 0,
    "discussion_timeout": 0,
    "teacher_freeze": 0,
    "submission": 0,
    "student_help": 1,
    "student_help_request": 1,
    "help_request": 1,
    "rule_high_risk": 2,
    "silence_check": 3,
    "post_intervention_observation": 5,
    "message_count_periodic": 6,
    "time_periodic": 6,
    "student_message": 6,
    "new_message": 6,
}

PRIORITY_BY_SUB_STATE = {
    "interpersonal_conflict": 2,
    "psychological_safety_risk": 2,
    "frustration": 3,
    "burnout": 3,
    "confusion": 3,
    "high_intensity_overload": 3,
    "off_topic_unregulated": 4,
    "perfunctory_detachment": 4,
    "individual_marginalization": 4,
    "off_topic_self_regulated": 7,
    "constructive_conflict": 7,
    "standard": 7,
    "deep_thinking": 7,
    "execution_progress": 7,
    "stage_achievement": 7,
    "unknown_sub_state": 7,
}

PRIORITY_BY_COARSE_STATE = {
    "EXPLICIT_HELP": 1,
    "POSSIBLE_CONFLICT": 2,
    "POSSIBLE_SILENCE": 3,
    "POSSIBLE_BLOCKED": 3,
    "POSSIBLE_DETACHMENT": 4,
    "POSSIBLE_PARTICIPATION_PROBLEM": 4,
    "NO_RISK": 7,
    "POSSIBLE_POSITIVE": 7,
    "UNKNOWN_COARSE": 7,
}


def priority_for(
    trigger_source: str = None,
    canonical_sub_state: str = None,
    coarse_state_code: str = None,
    *,
    stored_priority: Any = None,
    default: int = 6,
) -> int:
    """Return the deterministic coordination priority for a trigger/sub-state."""

    candidates: list[int] = []
    trigger = str(trigger_source or "").strip()
    canonical = str(canonical_sub_state or "").strip()
    coarse = str(coarse_state_code or "").strip()
    if trigger in PRIORITY_BY_TRIGGER:
        candidates.append(PRIORITY_BY_TRIGGER[trigger])
    if canonical in PRIORITY_BY_SUB_STATE:
        candidates.append(PRIORITY_BY_SUB_STATE[canonical])
    if coarse in PRIORITY_BY_COARSE_STATE:
        candidates.append(PRIORITY_BY_COARSE_STATE[coarse])
    try:
        if stored_priority is not None:
            candidates.append(int(stored_priority))
    except (TypeError, ValueError):
        pass
    return min(candidates) if candidates else int(default)


def priority_for_pipeline_row(row: Any) -> int:
    getter = row.get if hasattr(row, "get") else row.__getitem__
    return priority_for(
        getter("trigger_source"),
        getter("canonical_sub_state_code"),
        getter("coarse_state_code"),
        stored_priority=getter("trigger_priority"),
    )


def can_bypass_emotion_interval(row: Any) -> bool:
    getter = row.get if hasattr(row, "get") else row.__getitem__
    trigger = str(getter("trigger_source") or "")
    if trigger in HELP_TRIGGER_SOURCES:
        return True
    return priority_for_pipeline_row(row) <= 2


def supersede_lower_priority_runs_for_pipeline(pipeline_run_id: int) -> list[int]:
    """Supersede active lower-priority runs in the same group/session/discussion."""

    conn = db()
    try:
        if not _table_exists(conn, "strategy_pipeline_runs"):
            return []
        conn.execute("BEGIN IMMEDIATE")
        row = _load_pipeline(conn, int(pipeline_run_id))
        if not row:
            conn.rollback()
            return []
        superseded = supersede_lower_priority_runs_for_row(
            conn,
            row,
            reason="SUPERSEDED_BY_HIGHER_PRIORITY",
        )
        conn.commit()
        return superseded
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def supersede_preliminary_runs_for_batch(
    pipeline_run_id: int,
    *,
    start_sequence: int,
    end_sequence: int,
) -> list[int]:
    """Terminalize unfinished Stage 1 rows covered by one successful batch.

    Per-message and silence triggers may persist preliminary rows before the
    immutable assessment window finishes.  Once its authoritative Stage 2 row
    succeeds, those rows cannot be valid future work and must not keep a room
    lifecycle looking active indefinitely.
    """

    conn = db()
    try:
        if not _table_exists(conn, "strategy_pipeline_runs"):
            return []
        conn.execute("BEGIN IMMEDIATE")
        row = _load_pipeline(conn, int(pipeline_run_id))
        if not row:
            conn.rollback()
            return []
        superseded = supersede_preliminary_runs_for_batch_row(
            conn,
            row,
            start_sequence=int(start_sequence),
            end_sequence=int(end_sequence),
        )
        conn.commit()
        return superseded
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def supersede_preliminary_runs_for_batch_row(
    conn,
    row: Any,
    *,
    start_sequence: int,
    end_sequence: int,
) -> list[int]:
    """Keep the existing successful-batch supersede contract."""

    return finalize_preliminary_runs_for_batch_row(
        conn,
        row,
        start_sequence=start_sequence,
        end_sequence=end_sequence,
        assessment_batch_id=_row_value(row, "assessment_batch_id"),
        outcome="succeeded",
    )


def finalize_preliminary_runs_for_batch_row(
    conn,
    row: Any,
    *,
    start_sequence: int,
    end_sequence: int,
    assessment_batch_id: Optional[int],
    outcome: str,
    failure_code: Optional[str] = None,
    failure_detail: Optional[str] = None,
) -> list[int]:
    """Terminalize lock-free sibling rows owned by one terminal batch.

    A successful authoritative row supersedes preliminary siblings.  A
    terminally failed/quarantined authoritative row marks siblings failed
    without pretending that their own Stage 2 invocation ran.  Rows linked to
    another batch, published rows, and rows covered by a still-active batch are
    never changed.
    """

    normalized_outcome = str(outcome or "").strip().lower()
    if normalized_outcome not in {"succeeded", "failed"}:
        raise ValueError("unsupported_batch_pipeline_outcome")

    batch_id = (
        int(assessment_batch_id)
        if assessment_batch_id is not None
        else None
    )
    batch_filter = ""
    batch_params: tuple[Any, ...] = ()
    if batch_id is not None:
        batch_filter = """
          AND (
                p.assessment_batch_id=?
                OR (
                    p.assessment_batch_id IS NULL
                    AND NOT EXISTS (
                        SELECT 1
                        FROM state_assessment_batches AS active_batch
                        WHERE active_batch.group_id=p.group_id
                          AND COALESCE(active_batch.session_id, 0)
                              =COALESCE(p.session_id, 0)
                          AND COALESCE(active_batch.discussion_id, 0)
                              =COALESCE(p.discussion_id, 0)
                          AND active_batch.id<>?
                          AND active_batch.status IN ('pending','running')
                          AND COALESCE(p.input_cutoff_student_sequence, 0)
                              BETWEEN active_batch.candidate_start_sequence
                                  AND active_batch.candidate_end_sequence
                    )
                )
              )
        """
        batch_params = (batch_id, batch_id)

    candidates = conn.execute(
        f"""
        SELECT p.*
        FROM strategy_pipeline_runs AS p
        WHERE p.group_id=?
          AND COALESCE(p.session_id, 0)=COALESCE(?, 0)
          AND COALESCE(p.discussion_id, 0)=COALESCE(?, 0)
          AND p.id<>?
          AND COALESCE(p.input_cutoff_student_sequence, 0) BETWEEN ? AND ?
          AND UPPER(COALESCE(p.stage2_status, ''))<>'SUCCEEDED'
          AND UPPER(COALESCE(p.publish_status, ''))<>'PUBLISHED'
          AND UPPER(COALESCE(p.final_status, 'PENDING'))
              IN ({_placeholders(ACTIVE_FINAL_STATUSES)})
          {batch_filter}
        ORDER BY p.id ASC
        """,
        (
            row["group_id"],
            row["session_id"],
            row["discussion_id"],
            row["id"],
            int(start_sequence),
            int(end_sequence),
            *_ordered(ACTIVE_FINAL_STATUSES),
            *batch_params,
        ),
    ).fetchall()
    finalized: list[int] = []
    for candidate in candidates:
        successful = normalized_outcome == "succeeded"
        if _mark_pipeline_terminal(
            conn,
            candidate,
            final_status="SUPERSEDED" if successful else "FAILED",
            publish_status="SKIPPED",
            reason=(
                "SUPERSEDED_BY_STATE_BATCH"
                if successful
                else "COVERED_ASSESSMENT_BATCH_FAILED"
            ),
            superseded_by_run_id=int(row["id"]) if successful else None,
            assessment_batch_id=batch_id,
            assessment_owner_pipeline_run_id=int(row["id"]),
            stage2_status=None if successful else "SKIPPED",
            failure_code=None if successful else (
                str(failure_code or "assessment_batch_failed")
            ),
            failure_detail=None if successful else failure_detail,
        ):
            finalized.append(int(candidate["id"]))
    return finalized


def supersede_lower_priority_runs_for_row(conn, row: Any, *, reason: str) -> list[int]:
    current_priority = priority_for_pipeline_row(row)
    candidates = conn.execute(
        f"""
        SELECT *
        FROM strategy_pipeline_runs
        WHERE group_id=?
          AND COALESCE(session_id, 0)=COALESCE(?, 0)
          AND COALESCE(discussion_id, 0)=COALESCE(?, 0)
          AND id<>?
          AND COALESCE(publish_status, '')<>'PUBLISHED'
          AND COALESCE(final_status, 'PENDING') IN ({_placeholders(ACTIVE_FINAL_STATUSES)})
        ORDER BY COALESCE(trigger_priority, 999) ASC,
                 COALESCE(input_cutoff_student_sequence, 0) DESC,
                 id DESC
        """,
        (
            row["group_id"],
            row["session_id"],
            row["discussion_id"],
            row["id"],
            *_ordered(ACTIVE_FINAL_STATUSES),
        ),
    ).fetchall()
    superseded: list[int] = []
    for candidate in candidates:
        if priority_for_pipeline_row(candidate) <= current_priority:
            continue
        if _mark_pipeline_terminal(
            conn,
            candidate,
            final_status="SUPERSEDED",
            publish_status="SKIPPED",
            reason=reason,
            superseded_by_run_id=int(row["id"]),
        ):
            superseded.append(int(candidate["id"]))
    return superseded


def preempt_for_student_help(help_request_id: int) -> list[int]:
    """Let an active help request preempt unpublished lower-priority strategy work."""

    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        help_row = conn.execute(
            "SELECT * FROM help_requests WHERE id=?",
            (int(help_request_id),),
        ).fetchone()
        if not help_row:
            conn.rollback()
            return []
        if not _table_exists(conn, "strategy_pipeline_runs"):
            conn.commit()
            return []
        candidates = _active_runs_in_scope(
            conn,
            group_id=help_row["group_id"],
            session_id=help_row["session_id"],
            discussion_id=help_row["discussion_id"],
        )
        superseded: list[int] = []
        for candidate in candidates:
            if priority_for_pipeline_row(candidate) <= 1:
                continue
            if _mark_pipeline_terminal(
                conn,
                candidate,
                final_status="SUPERSEDED",
                publish_status="SKIPPED",
                reason="SUPERSEDED_BY_STUDENT_HELP",
                superseded_by_run_id=None,
                replacement_trigger_message_id=help_row["source_message_id"],
                replacement_cutoff_sequence=help_row["help_request_message_sequence"],
            ):
                superseded.append(int(candidate["id"]))
        conn.commit()
        return superseded
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cancel_active_runs_for_session_end(session_id: int, *, reason: str = "CANCELLED_SESSION_ENDED") -> list[int]:
    """Cancel active unpublished strategy runs for an ended session and free locks."""

    conn = db()
    try:
        if not _table_exists(conn, "strategy_pipeline_runs"):
            return []
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"""
            SELECT *
            FROM strategy_pipeline_runs
            WHERE session_id=?
              AND COALESCE(publish_status, '')<>'PUBLISHED'
              AND COALESCE(final_status, 'PENDING') IN ({_placeholders(ACTIVE_FINAL_STATUSES)})
            ORDER BY id ASC
            """,
            (int(session_id), *_ordered(ACTIVE_FINAL_STATUSES)),
        ).fetchall()
        cancelled: list[int] = []
        timestamp = now_str()
        for row in rows:
            if _mark_pipeline_terminal(
                conn,
                row,
                final_status="CANCELLED",
                publish_status="SKIPPED",
                reason=reason,
                superseded_by_run_id=None,
                timestamp=timestamp,
            ):
                cancelled.append(int(row["id"]))
        conn.commit()
        return cancelled
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def active_emotion_slot_blocks_strategy(conn, row: Any) -> Optional[dict]:
    """Deprecated no-op: Agent modes are mutually exclusive."""
    return None


def emotion_strategy_conflict_check(
    group_id: int,
    session_id: int,
    discussion_id: int,
    *,
    now_text: str = None,
    min_interval_seconds: int = None,
) -> dict:
    """Deprecated no-op retained for old imports and historical tools."""
    return {"allowed": True, "reason": "mutually_exclusive_agent_modes"}


def _active_runs_in_scope(conn, *, group_id: int, session_id: Any, discussion_id: Any):
    return conn.execute(
        f"""
        SELECT *
        FROM strategy_pipeline_runs
        WHERE group_id=?
          AND COALESCE(session_id, 0)=COALESCE(?, 0)
          AND COALESCE(discussion_id, 0)=COALESCE(?, 0)
          AND COALESCE(publish_status, '')<>'PUBLISHED'
          AND COALESCE(final_status, 'PENDING') IN ({_placeholders(ACTIVE_FINAL_STATUSES)})
        ORDER BY COALESCE(trigger_priority, 999) ASC,
                 COALESCE(input_cutoff_student_sequence, 0) DESC,
                 id DESC
        """,
        (int(group_id), session_id, discussion_id, *_ordered(ACTIVE_FINAL_STATUSES)),
    ).fetchall()


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        keys = row.keys()
        if key not in keys:
            return default
    except AttributeError:
        pass
    try:
        return row.get(key, default) if hasattr(row, "get") else row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _replacement_trigger_message(conn, row: Any, cutoff_sequence: Any = None):
    explicit_id = _row_value(row, "trigger_message_id")
    if explicit_id is not None:
        try:
            return int(explicit_id)
        except (TypeError, ValueError):
            pass
    cutoff = cutoff_sequence
    if cutoff is None:
        cutoff = _row_value(row, "input_cutoff_student_sequence")
    if cutoff is None:
        return None
    try:
        cutoff = int(cutoff)
    except (TypeError, ValueError):
        return None
    message = conn.execute(
        """
        SELECT m.id
        FROM messages AS m
        LEFT JOIN users AS u ON u.id=m.user_id
        WHERE m.group_id=?
          AND COALESCE(m.session_id, 0)=COALESCE(?, 0)
          AND COALESCE(m.discussion_id, 0)=COALESCE(?, 0)
          AND COALESCE(NULLIF(TRIM(m.role), ''), m.sender_type, u.role)='student'
          AND m.sequence IS NOT NULL
          AND m.sequence<=?
        ORDER BY m.sequence DESC, m.id DESC
        LIMIT 1
        """,
        (
            _row_value(row, "group_id"),
            _row_value(row, "session_id"),
            _row_value(row, "discussion_id"),
            cutoff,
        ),
    ).fetchone()
    return int(message["id"]) if message else None


def record_replacement_request(
    conn,
    pipeline_run_id: int,
    *,
    reason: str,
    trigger_message_id: Optional[int] = None,
    cutoff_sequence: Optional[int] = None,
) -> dict:
    """Persist a replacement request before its asynchronous pipeline exists."""

    row = _load_pipeline(conn, int(pipeline_run_id))
    if not row:
        return {"recorded": False, "reason": "pipeline_not_found"}
    cutoff = cutoff_sequence
    if cutoff is None:
        cutoff = _row_value(row, "input_cutoff_student_sequence")
    trigger_id = trigger_message_id
    if trigger_id is None:
        trigger_id = _replacement_trigger_message(conn, row, cutoff)
    conn.execute(
        """
        UPDATE strategy_pipeline_runs
           SET replacement_reason=COALESCE(?, replacement_reason),
               replacement_trigger_message_id=COALESCE(?, replacement_trigger_message_id),
               replacement_cutoff_sequence=COALESCE(?, replacement_cutoff_sequence),
               updated_at=?
         WHERE id=?
        """,
        (
            str(reason or "replacement_requested"),
            trigger_id,
            cutoff,
            now_str(),
            int(pipeline_run_id),
        ),
    )
    return {
        "recorded": True,
        "original_pipeline_run_id": int(pipeline_run_id),
        "replacement_reason": str(reason or "replacement_requested"),
        "replacement_trigger_message_id": trigger_id,
        "replacement_cutoff_sequence": cutoff,
        "replaced_by_pipeline_run_id": _row_value(row, "replaced_by_pipeline_run_id"),
    }


def sync_latest_state_from_replacement(
    conn,
    original_pipeline_run_id: int,
    replacement_pipeline_run_id: int,
) -> dict:
    """Project replacement state onto the retired row without defaulting unknown to false."""

    replacement = _load_pipeline(conn, int(replacement_pipeline_run_id))
    if not replacement:
        return {"updated": False, "reason": "replacement_pipeline_not_found"}
    stage2_succeeded = str(_row_value(replacement, "stage2_status") or "").upper() == "SUCCEEDED"
    latest_state = (
        str(_row_value(replacement, "canonical_sub_state_code") or "").strip()
        if stage2_succeeded
        else ""
    ) or None
    latest_should_intervene = (
        _row_value(replacement, "should_intervene") if stage2_succeeded else None
    )
    conn.execute(
        """
        UPDATE strategy_pipeline_runs
           SET latest_state=?,
               latest_should_intervene=?,
               latest_state_pipeline_run_id=?,
               updated_at=?
         WHERE id=?
        """,
        (
            latest_state,
            latest_should_intervene,
            int(replacement_pipeline_run_id),
            now_str(),
            int(original_pipeline_run_id),
        ),
    )
    return {
        "updated": True,
        "original_pipeline_run_id": int(original_pipeline_run_id),
        "replacement_pipeline_run_id": int(replacement_pipeline_run_id),
        "latest_state": latest_state,
        "latest_should_intervene": latest_should_intervene,
    }


def link_pipeline_replacement(
    conn,
    original_pipeline_run_id: int,
    replacement_pipeline_run_id: int,
    *,
    reason: str = None,
    trigger_message_id: Optional[int] = None,
    cutoff_sequence: Optional[int] = None,
) -> dict:
    """Link an old terminal row to its replacement in one transaction."""

    original_id = int(original_pipeline_run_id)
    replacement_id = int(replacement_pipeline_run_id)
    if original_id == replacement_id:
        return {"linked": False, "reason": "same_pipeline"}
    original = _load_pipeline(conn, original_id)
    replacement = _load_pipeline(conn, replacement_id)
    if not original or not replacement:
        return {"linked": False, "reason": "pipeline_not_found"}
    replacement_reason = (
        reason
        or _row_value(replacement, "replacement_reason")
        or _row_value(original, "replacement_reason")
        or "replacement_pipeline_created"
    )
    cutoff = cutoff_sequence
    if cutoff is None:
        cutoff = _row_value(replacement, "replacement_cutoff_sequence")
    if cutoff is None:
        cutoff = _row_value(replacement, "input_cutoff_student_sequence")
    trigger_id = trigger_message_id
    if trigger_id is None:
        trigger_id = _row_value(replacement, "replacement_trigger_message_id")
    if trigger_id is None:
        trigger_id = _replacement_trigger_message(conn, replacement, cutoff)
    conn.execute(
        """
        UPDATE strategy_pipeline_runs
           SET replaced_by_pipeline_run_id=COALESCE(replaced_by_pipeline_run_id, ?),
               replacement_reason=COALESCE(replacement_reason, ?),
               replacement_trigger_message_id=COALESCE(replacement_trigger_message_id, ?),
               replacement_cutoff_sequence=COALESCE(replacement_cutoff_sequence, ?),
               updated_at=?
         WHERE id=?
        """,
        (
            replacement_id,
            str(replacement_reason),
            trigger_id,
            cutoff,
            now_str(),
            original_id,
        ),
    )
    conn.execute(
        """
        UPDATE strategy_pipeline_runs
           SET parent_run_id=COALESCE(parent_run_id, ?),
               trigger_message_id=COALESCE(trigger_message_id, ?),
               updated_at=?
         WHERE id=?
        """,
        (original_id, trigger_id, now_str(), replacement_id),
    )
    latest = sync_latest_state_from_replacement(conn, original_id, replacement_id)
    return {
        "linked": True,
        "original_pipeline_run_id": original_id,
        "replacement_pipeline_run_id": replacement_id,
        "replacement_reason": str(replacement_reason),
        "replacement_trigger_message_id": trigger_id,
        "replacement_cutoff_sequence": cutoff,
        "latest_state": latest.get("latest_state"),
    }


def _mark_pipeline_terminal(
    conn,
    row: Any,
    *,
    final_status: str,
    publish_status: str,
    reason: str,
    superseded_by_run_id: Optional[int] = None,
    assessment_batch_id: Optional[int] = None,
    assessment_owner_pipeline_run_id: Optional[int] = None,
    stage2_status: Optional[str] = None,
    failure_code: Optional[str] = None,
    failure_detail: Optional[str] = None,
    replacement_trigger_message_id: Optional[int] = None,
    replacement_cutoff_sequence: Optional[int] = None,
    timestamp: str = None,
) -> bool:
    if row["publish_status"] == "PUBLISHED" or row["final_status"] in TERMINAL_FINAL_STATUSES:
        return False
    timestamp = timestamp or now_str()
    replacement_row = (
        _load_pipeline(conn, int(superseded_by_run_id))
        if superseded_by_run_id is not None
        else None
    )
    replacement_cutoff = replacement_cutoff_sequence
    if replacement_cutoff is None and replacement_row:
        replacement_cutoff = _row_value(
            replacement_row, "input_cutoff_student_sequence"
        )
    replacement_trigger = replacement_trigger_message_id
    if replacement_trigger is None and replacement_row:
        replacement_trigger = _replacement_trigger_message(
            conn, replacement_row, replacement_cutoff
        )
    released = _release_pipeline_lock(conn, row, timestamp)
    conn.execute(
        """
        UPDATE strategy_pipeline_runs
           SET publish_status=?,
               final_status=?,
               skip_reason=?,
               assessment_batch_id=COALESCE(assessment_batch_id, ?),
               assessment_owner_pipeline_run_id=COALESCE(
                   assessment_owner_pipeline_run_id, ?
               ),
               stage2_status=CASE
                   WHEN ? IS NULL THEN stage2_status
                   ELSE ?
               END,
               stage2_completed_at=CASE
                   WHEN ? IS NULL THEN stage2_completed_at
                   ELSE COALESCE(stage2_completed_at, ?)
               END,
               failure_code=CASE
                   WHEN ? IS NULL THEN failure_code
                   ELSE ?
               END,
               failure_detail=CASE
                   WHEN ? IS NULL THEN failure_detail
                   ELSE ?
               END,
               should_intervene=CASE
                   WHEN UPPER(COALESCE(stage2_status, ''))='SUCCEEDED'
                       THEN should_intervene
                   ELSE NULL
               END,
               latest_should_intervene=CASE
                   WHEN UPPER(COALESCE(stage2_status, ''))='SUCCEEDED'
                       THEN latest_should_intervene
                   ELSE NULL
               END,
               superseded_by_run_id=COALESCE(?, superseded_by_run_id),
               replaced_by_pipeline_run_id=COALESCE(?, replaced_by_pipeline_run_id),
               replacement_reason=CASE
                   WHEN ? IS NULL THEN replacement_reason
                   ELSE COALESCE(replacement_reason, ?)
               END,
               replacement_trigger_message_id=COALESCE(?, replacement_trigger_message_id),
               replacement_cutoff_sequence=COALESCE(?, replacement_cutoff_sequence),
               room_lock_released_at=COALESCE(room_lock_released_at, ?),
               updated_at=?
         WHERE id=?
        """,
        (
            publish_status,
            final_status,
            reason,
            assessment_batch_id,
            assessment_owner_pipeline_run_id,
            stage2_status,
            stage2_status,
            stage2_status,
            timestamp,
            failure_code,
            failure_code,
            failure_detail,
            failure_detail,
            superseded_by_run_id,
            superseded_by_run_id,
            superseded_by_run_id,
            reason,
            replacement_trigger,
            replacement_cutoff,
            timestamp if released else None,
            timestamp,
            int(row["id"]),
        ),
    )
    if superseded_by_run_id is not None:
        link_pipeline_replacement(
            conn,
            int(row["id"]),
            int(superseded_by_run_id),
            reason=reason,
            trigger_message_id=replacement_trigger,
            cutoff_sequence=replacement_cutoff,
        )
    conn.execute(
        """
        UPDATE intervention_runs
           SET status=?,
               decision='SKIPPED',
               publish_status=?,
               skip_reason=?,
               failure_reason=COALESCE(failure_reason, ?),
               completed_at=?,
               lock_acquired=0
         WHERE strategy_pipeline_run_id=?
           AND COALESCE(publish_status, '')<>'PUBLISHED'
        """,
        (
            final_status,
            publish_status,
            reason,
            reason,
            timestamp,
            int(row["id"]),
        ),
    )
    return True


def _release_pipeline_lock(conn, row: Any, timestamp: str) -> bool:
    token = row["room_lock_token"] if "room_lock_token" in row.keys() else None
    if not token:
        return False
    cur = conn.execute(
        """
        UPDATE groups
           SET state='OPEN',
               version=version+1,
               lock_token=NULL,
               lock_expires_at=NULL,
               active_intervention_run_id=NULL
         WHERE id=? AND lock_token=? AND active_intervention_run_id=?
        """,
        (row["group_id"], token, -int(row["id"])),
    )
    released = cur.rowcount == 1
    if released:
        record_latency_event(
            stage="lock",
            event="room_lock_released",
            pipeline_run_id=row["id"],
            assessment_batch_id=(
                row["assessment_batch_id"]
                if "assessment_batch_id" in row.keys()
                else None
            ),
            occurred_at=timestamp,
            lock_token=token,
            details={
                "reason": "coordination_terminal",
                "lease_action": "release",
                "lease_released": True,
            },
            conn=conn,
            pipeline_context=True,
        )
    return released


def _load_pipeline(conn, pipeline_run_id: int):
    return conn.execute(
        "SELECT * FROM strategy_pipeline_runs WHERE id=?",
        (int(pipeline_run_id),),
    ).fetchone()


def _table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _ordered(values: set[str]) -> tuple[str, ...]:
    return tuple(sorted(values))


def _placeholders(values: set[str]) -> str:
    return ",".join("?" for _ in values)


__all__ = [
    "ACTIVE_FINAL_STATUSES",
    "AGENT_CROSS_CHANNEL_MIN_INTERVAL_SECONDS",
    "HELP_TRIGGER_SOURCES",
    "TERMINAL_FINAL_STATUSES",
    "active_emotion_slot_blocks_strategy",
    "can_bypass_emotion_interval",
    "cancel_active_runs_for_session_end",
    "emotion_strategy_conflict_check",
    "preempt_for_student_help",
    "priority_for",
    "priority_for_pipeline_row",
    "supersede_lower_priority_runs_for_pipeline",
    "supersede_preliminary_runs_for_batch",
    "supersede_preliminary_runs_for_batch_row",
    "finalize_preliminary_runs_for_batch_row",
    "supersede_lower_priority_runs_for_row",
]
