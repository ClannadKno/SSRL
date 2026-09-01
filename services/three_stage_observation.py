# -*- coding: utf-8 -*-
"""Post-intervention observation bookkeeping for three-stage strategy runs."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Optional

from config import OBSERVATION_MAX_ASSESSMENT_ROUNDS
from db import db, now_str
from services.three_stage_schema import dumps_json, normalize_canonical_sub_state


RECOVERY_SUB_STATES = {
    "standard",
    "deep_thinking",
    "execution_progress",
    "constructive_conflict",
    "stage_achievement",
}
TERMINAL_OBSERVATION_RESULTS = {
    "recovered",
    "persistent_risk",
    "migrated_risk",
    "state_migrated_non_intervention",
    "window_expired_no_recovery",
    "finalization_only",
}
MIN_RECOVERY_STUDENT_MESSAGES = 2


def mark_observation_started(
    conn,
    *,
    pipeline_row,
    message_id: int,
    timestamp: str = None,
) -> dict:
    """Mark the published pipeline as waiting for post-intervention feedback."""
    if not pipeline_row or not message_id:
        return {"updated": False, "reason": "missing_pipeline_or_message"}
    message = conn.execute(
        "SELECT id, sequence, created_at FROM messages WHERE id=?",
        (int(message_id),),
    ).fetchone()
    if not message or message["sequence"] is None:
        return {"updated": False, "reason": "published_message_not_found"}

    published_sequence = int(message["sequence"])
    timestamp = timestamp or now_str()
    details = {
        "schema_version": "post_intervention_observation.v1",
        "published_message_id": int(message_id),
        "published_sequence": published_sequence,
        "previous_sub_state": pipeline_row["canonical_sub_state_code"],
        "selected_strategy_id": pipeline_row["selected_strategy_id"],
    }
    conn.execute(
        """
        UPDATE strategy_pipeline_runs
        SET observation_status='observing',
            observation_started_at=COALESCE(observation_started_at, ?),
            observation_window_start_sequence=COALESCE(
                observation_window_start_sequence,
                ?
            ),
            observation_previous_sub_state_code=COALESCE(
                observation_previous_sub_state_code,
                canonical_sub_state_code
            ),
            observation_details_json=?,
            updated_at=?
        WHERE id=?
        """,
        (
            timestamp,
            published_sequence + 1,
            dumps_json(details),
            timestamp,
            int(pipeline_row["id"]),
        ),
    )
    return {
        "updated": True,
        "pipeline_run_id": int(pipeline_row["id"]),
        "published_sequence": published_sequence,
        "observation_window_start_sequence": published_sequence + 1,
    }


def enrich_state_detector_context(
    context: dict,
    *,
    group_id: int,
    session_id: int,
    discussion_id: int,
    cutoff_sequence: int = None,
) -> dict:
    """Attach previous intervention state/strategy to Stage2 observation context."""
    enriched = dict(context or {})
    if not session_id or not discussion_id:
        return enriched
    conn = db()
    try:
        observed = _find_observed_pipeline(
            conn,
            group_id=group_id,
            session_id=session_id,
            discussion_id=discussion_id,
            before_sequence=cutoff_sequence,
        )
        if not observed:
            return enriched
        first_response = _first_student_response_after(conn, observed)
        info = _observation_context_payload(observed, first_response)
        enriched["post_intervention_observation"] = info
        recent = dict(enriched.get("recent_intervention") or {})
        recent.update(
            {
                "pipeline_run_id": info["previous_pipeline_run_id"],
                "published_message_id": info["published_message_id"],
                "sequence": info["published_sequence"],
                "strategy_id": info["selected_strategy_id"],
                "selected_strategy_id": info["selected_strategy_id"],
                "canonical_sub_state_code": info["previous_sub_state"],
                "content": info.get("published_message_text"),
                "created_at": info.get("published_at"),
            }
        )
        enriched["recent_intervention"] = recent
        return enriched
    finally:
        conn.close()


def record_observation_assessment(
    *,
    observation_pipeline_run_id: int,
    batch: dict,
    stage2_result: dict,
) -> dict:
    """Summarize a post-intervention Stage2 assessment onto the prior run."""
    if not batch or batch.get("trigger_type") != "post_intervention_observation":
        return {"updated": False, "reason": "not_observation_batch"}
    if not observation_pipeline_run_id:
        return {"updated": False, "reason": "missing_observation_pipeline"}

    timestamp = now_str()
    conn = db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        observed = _find_observed_pipeline(
            conn,
            group_id=batch["group_id"],
            session_id=batch["session_id"],
            discussion_id=batch["discussion_id"],
            before_sequence=batch["candidate_start_sequence"],
        )
        if not observed:
            conn.commit()
            return {
                "updated": False,
                "reason": "observed_pipeline_not_found",
                "observation_pipeline_run_id": int(observation_pipeline_run_id),
            }

        active = dict((stage2_result or {}).get("active_sub_state") or {})
        current_sub_state = normalize_canonical_sub_state(
            active.get("canonical_sub_state")
        )
        previous_sub_state = normalize_canonical_sub_state(
            observed["canonical_sub_state_code"]
        )
        should_intervene = bool((stage2_result or {}).get("should_intervene"))
        first_response = _first_student_response_after(conn, observed)
        student_count = _student_response_count(
            conn,
            observed,
            through_sequence=batch.get("candidate_end_sequence"),
        )
        observation_rounds = _completed_observation_rounds(conn, observed)
        result = _classify_observation_result(
            previous_sub_state=previous_sub_state,
            current_sub_state=current_sub_state,
            should_intervene=should_intervene,
            student_count=student_count,
            observation_rounds=observation_rounds,
        )
        terminal = result in TERMINAL_OBSERVATION_RESULTS
        details = _observation_details(
            observed=observed,
            batch=batch,
            stage2_result=stage2_result,
            first_response=first_response,
            student_count=student_count,
            observation_rounds=observation_rounds,
            result=result,
        )
        first_response_seconds = _seconds_between(
            observed["published_at"] or observed["published_message_created_at"],
            first_response["created_at"] if first_response else None,
        )
        reintervention_run_id = (
            int(observation_pipeline_run_id) if should_intervene else None
        )
        conn.execute(
            """
            UPDATE strategy_pipeline_runs
            SET observation_status=?,
                observation_first_response_sequence=COALESCE(
                    observation_first_response_sequence,
                    ?
                ),
                observation_first_response_seconds=COALESCE(
                    observation_first_response_seconds,
                    ?
                ),
                observation_window_start_sequence=COALESCE(
                    observation_window_start_sequence,
                    ?
                ),
                observation_window_end_sequence=?,
                observation_completed_at=CASE WHEN ?=1 THEN ? ELSE observation_completed_at END,
                observation_result=?,
                observation_assessment_run_id=?,
                observation_assessment_batch_id=?,
                observation_reintervention_run_id=COALESCE(
                    ?,
                    observation_reintervention_run_id
                ),
                observation_previous_sub_state_code=COALESCE(
                    observation_previous_sub_state_code,
                    ?
                ),
                observation_current_sub_state_code=?,
                observation_details_json=?,
                updated_at=?
            WHERE id=?
            """,
            (
                "completed" if terminal else "observing",
                first_response["sequence"] if first_response else None,
                first_response_seconds,
                first_response["sequence"] if first_response else None,
                batch.get("candidate_end_sequence"),
                1 if terminal else 0,
                timestamp,
                result,
                int(observation_pipeline_run_id),
                int(batch["id"]),
                reintervention_run_id,
                previous_sub_state,
                current_sub_state,
                dumps_json(details),
                timestamp,
                int(observed["id"]),
            ),
        )
        conn.execute(
            """
            UPDATE strategy_pipeline_runs
            SET parent_run_id=COALESCE(parent_run_id, ?),
                updated_at=?
            WHERE id=?
            """,
            (int(observed["id"]), timestamp, int(observation_pipeline_run_id)),
        )
        if terminal:
            _deactivate_observation_cursor(conn, observed, timestamp)
        conn.commit()
        return {
            "updated": True,
            "observed_pipeline_run_id": int(observed["id"]),
            "observation_pipeline_run_id": int(observation_pipeline_run_id),
            "observation_result": result,
            "observation_status": "completed" if terminal else "observing",
            "first_response_sequence": first_response["sequence"]
            if first_response
            else None,
            "student_response_count": student_count,
            "observation_rounds": observation_rounds,
            "reintervention_run_id": reintervention_run_id,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_session_observations_finalization_only(
    session_id: int,
    *,
    reason: str = "session_end",
) -> dict:
    """Close still-observing published runs without creating interventions."""
    timestamp = now_str()
    conn = db()
    try:
        if not _table_exists(conn, "strategy_pipeline_runs"):
            return {"updated": 0, "reason": "strategy_pipeline_runs_missing"}
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT id, observation_details_json
            FROM strategy_pipeline_runs
            WHERE session_id=?
              AND publish_status='PUBLISHED'
              AND observation_status='observing'
            """,
            (int(session_id),),
        ).fetchall()
        for row in rows:
            details = _json_object(row["observation_details_json"])
            details["finalization_reason"] = reason
            details["finalized_at"] = timestamp
            conn.execute(
                """
                UPDATE strategy_pipeline_runs
                SET observation_status='finalization_only',
                    observation_result='finalization_only',
                    observation_completed_at=?,
                    observation_details_json=?,
                    updated_at=?
                WHERE id=?
                """,
                (timestamp, dumps_json(details), timestamp, row["id"]),
            )
        conn.execute(
            """
            UPDATE discussion_assessment_cursors
            SET observation_status='inactive', updated_at=?
            WHERE session_id=? AND observation_status='observing'
            """,
            (timestamp, int(session_id)),
        )
        conn.commit()
        return {"updated": len(rows), "reason": reason}
    except sqlite3.OperationalError as exc:
        conn.rollback()
        if "no such table" in str(exc) or "no such column" in str(exc):
            return {"updated": 0, "reason": "legacy_schema_missing"}
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _find_observed_pipeline(
    conn,
    *,
    group_id: int,
    session_id: int,
    discussion_id: int,
    before_sequence: int = None,
):
    before_clause = ""
    params: list[Any] = [group_id, session_id, discussion_id]
    if before_sequence is not None:
        before_clause = "AND COALESCE(m.sequence, 0) < ?"
        params.append(int(before_sequence))
    return conn.execute(
        f"""
        SELECT spr.*, m.sequence AS published_sequence,
               m.created_at AS published_message_created_at,
               m.content AS published_message_text
        FROM strategy_pipeline_runs AS spr
        LEFT JOIN messages AS m ON m.id=spr.published_message_id
        WHERE spr.group_id=?
          AND COALESCE(spr.session_id, 0)=COALESCE(?, 0)
          AND COALESCE(spr.discussion_id, 0)=COALESCE(?, 0)
          AND spr.publish_status='PUBLISHED'
          AND spr.observation_status='observing'
          {before_clause}
        ORDER BY COALESCE(m.sequence, spr.input_cutoff_student_sequence, 0) DESC,
                 spr.id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()


def _first_student_response_after(conn, observed):
    sequence = observed["published_sequence"]
    if sequence is None:
        return None
    row = conn.execute(
        """
        SELECT m.sequence, m.created_at
        FROM messages AS m
        LEFT JOIN users AS u ON u.id=m.user_id
        WHERE m.group_id=?
          AND COALESCE(m.session_id, 0)=COALESCE(?, 0)
          AND COALESCE(m.discussion_id, 0)=COALESCE(?, 0)
          AND COALESCE(NULLIF(TRIM(m.role), ''), m.sender_type, u.role)='student'
          AND m.sequence>?
        ORDER BY m.sequence ASC, m.id ASC
        LIMIT 1
        """,
        (
            observed["group_id"],
            observed["session_id"],
            observed["discussion_id"],
            int(sequence),
        ),
    ).fetchone()
    return dict(row) if row else None


def _student_response_count(conn, observed, *, through_sequence: int = None) -> int:
    sequence = observed["published_sequence"]
    if sequence is None:
        return 0
    params: list[Any] = [
        observed["group_id"],
        observed["session_id"],
        observed["discussion_id"],
        int(sequence),
    ]
    through_clause = ""
    if through_sequence is not None:
        through_clause = "AND m.sequence<=?"
        params.append(int(through_sequence))
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM messages AS m
        LEFT JOIN users AS u ON u.id=m.user_id
        WHERE m.group_id=?
          AND COALESCE(m.session_id, 0)=COALESCE(?, 0)
          AND COALESCE(m.discussion_id, 0)=COALESCE(?, 0)
          AND COALESCE(NULLIF(TRIM(m.role), ''), m.sender_type, u.role)='student'
          AND m.sequence>?
          {through_clause}
        """,
        tuple(params),
    ).fetchone()
    return int(row["count"] or 0) if row else 0


def _completed_observation_rounds(conn, observed) -> int:
    sequence = observed["published_sequence"]
    if sequence is None:
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM state_assessment_batches
        WHERE group_id=?
          AND COALESCE(session_id, 0)=COALESCE(?, 0)
          AND COALESCE(discussion_id, 0)=COALESCE(?, 0)
          AND trigger_type='post_intervention_observation'
          AND status='succeeded'
          AND candidate_end_sequence>?
        """,
        (
            observed["group_id"],
            observed["session_id"],
            observed["discussion_id"],
            int(sequence),
        ),
    ).fetchone()
    return int(row["count"] or 0) if row else 0


def _classify_observation_result(
    *,
    previous_sub_state: Optional[str],
    current_sub_state: Optional[str],
    should_intervene: bool,
    student_count: int,
    observation_rounds: int,
) -> str:
    if not current_sub_state or current_sub_state == "unknown_sub_state":
        if int(observation_rounds or 0) >= int(OBSERVATION_MAX_ASSESSMENT_ROUNDS):
            return "window_expired_no_recovery"
        return "continue_observing"
    if current_sub_state in RECOVERY_SUB_STATES:
        if (
            int(student_count or 0) >= MIN_RECOVERY_STUDENT_MESSAGES
            or int(observation_rounds or 0) >= int(OBSERVATION_MAX_ASSESSMENT_ROUNDS)
        ):
            return "recovered"
        return "insufficient_evidence"
    if should_intervene:
        if current_sub_state == previous_sub_state:
            return "persistent_risk"
        return "migrated_risk"
    if current_sub_state != previous_sub_state:
        return "state_migrated_non_intervention"
    if int(observation_rounds or 0) >= int(OBSERVATION_MAX_ASSESSMENT_ROUNDS):
        return "window_expired_no_recovery"
    return "continue_observing"


def _observation_context_payload(observed, first_response: dict = None) -> dict:
    return {
        "schema_version": "post_intervention_observation.v1",
        "previous_pipeline_run_id": int(observed["id"]),
        "previous_sub_state": observed["canonical_sub_state_code"],
        "selected_strategy_id": observed["selected_strategy_id"],
        "published_message_id": observed["published_message_id"],
        "published_sequence": observed["published_sequence"],
        "published_at": observed["published_at"]
        or observed["published_message_created_at"],
        "published_message_text": observed["published_message_text"],
        "observation_started_at": observed["observation_started_at"],
        "observation_window_start_sequence": observed[
            "observation_window_start_sequence"
        ],
        "first_response_sequence": first_response["sequence"]
        if first_response
        else observed["observation_first_response_sequence"],
    }


def _observation_details(
    *,
    observed,
    batch: dict,
    stage2_result: dict,
    first_response: dict = None,
    student_count: int,
    observation_rounds: int,
    result: str,
) -> dict:
    return {
        "schema_version": "post_intervention_observation.v1",
        "result": result,
        "previous_pipeline_run_id": int(observed["id"]),
        "previous_sub_state": observed["canonical_sub_state_code"],
        "selected_strategy_id": observed["selected_strategy_id"],
        "published_message_id": observed["published_message_id"],
        "published_sequence": observed["published_sequence"],
        "first_response_sequence": first_response["sequence"]
        if first_response
        else None,
        "student_response_count": int(student_count or 0),
        "observation_rounds": int(observation_rounds or 0),
        "window_start_sequence": batch.get("candidate_start_sequence"),
        "window_end_sequence": batch.get("candidate_end_sequence"),
        "current_sub_state": (
            (stage2_result or {}).get("active_sub_state") or {}
        ).get("canonical_sub_state"),
        "should_intervene": bool((stage2_result or {}).get("should_intervene")),
        "all_state_segments": (stage2_result or {}).get("segments") or [],
    }


def _deactivate_observation_cursor(conn, observed, timestamp: str) -> None:
    conn.execute(
        """
        UPDATE discussion_assessment_cursors
        SET observation_status='inactive', updated_at=?
        WHERE group_id=?
          AND COALESCE(session_id, 0)=COALESCE(?, 0)
          AND COALESCE(discussion_id, 0)=COALESCE(?, 0)
          AND observation_status='observing'
        """,
        (
            timestamp,
            observed["group_id"],
            observed["session_id"],
            observed["discussion_id"],
        ),
    )


def _seconds_between(start: Any, end: Any) -> Optional[float]:
    start_dt = _parse_dt(start)
    end_dt = _parse_dt(end)
    if not start_dt or not end_dt:
        return None
    return max(0.0, (end_dt - start_dt).total_seconds())


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except (TypeError, ValueError):
        return None


def _json_object(value: Any) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


__all__ = [
    "RECOVERY_SUB_STATES",
    "enrich_state_detector_context",
    "mark_observation_started",
    "mark_session_observations_finalization_only",
    "record_observation_assessment",
]
