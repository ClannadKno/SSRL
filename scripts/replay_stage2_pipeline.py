"""Read-only replay of the persisted Stage 2 response shape.

This command deliberately never imports the scheduler, creates a pipeline, or
publishes a message.  Historical rows may contain a provider envelope, so only
its assistant-content shape is passed to the local parser and only bounded
diagnostic fields are printed.
"""

from __future__ import annotations

import argparse
import json
import sys
import sqlite3
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.discussion_pipeline_v2.llm_state_detector import (
    replay_stage2_response,
)


_SAFE_EVENT_FIELDS = {
    "attempt_type",
    "failure_category",
    "finish_reason",
    "json_extractable",
    "response_character_count",
    "response_incomplete",
    "stage2_failure_category",
    "success",
}


def _read_only_connection(database_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)


def _json_value(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _candidate_sequences(batch: sqlite3.Row | None) -> list[int]:
    if not batch:
        return []
    values = _json_value(batch["student_sequences_json"])
    if isinstance(values, list):
        result = []
        for value in values:
            try:
                result.append(int(value))
            except (TypeError, ValueError):
                continue
        if result:
            return result
    result = []
    for field in ("candidate_start_sequence", "candidate_end_sequence"):
        try:
            value = int(batch[field])
        except (TypeError, ValueError):
            continue
        if value not in result:
            result.append(value)
    return result


def _safe_event_evidence(rows: list[sqlite3.Row]) -> list[dict]:
    evidence = []
    for row in rows:
        details = _json_value(row["details_json"], {})
        if not isinstance(details, dict):
            details = {}
        safe_details = {
            key: details[key]
            for key in _SAFE_EVENT_FIELDS
            if key in details
        }
        if safe_details or row["event"] in {
            "stage2_llm_started",
            "stage2_llm_finished",
            "stage2_finished",
        }:
            evidence.append(
                {
                    "event": row["event"],
                    "occurred_at": row["occurred_at"],
                    "details": safe_details,
                }
            )
    return evidence


def replay_pipeline(database_path: Path, pipeline_id: int) -> dict:
    """Return a bounded replay report without any database write."""

    conn = _read_only_connection(database_path)
    conn.row_factory = sqlite3.Row
    try:
        pipeline = conn.execute(
            """
            SELECT id, stage2_status, failure_code, stage2_completed_at,
                   state_raw_response_json, assessment_batch_id
            FROM strategy_pipeline_runs
            WHERE id=?
            """,
            (int(pipeline_id),),
        ).fetchone()
        if pipeline is None:
            return {
                "read_only": True,
                "pipeline_run_id": int(pipeline_id),
                "status": "not_found",
                "side_effects": {
                    "database_writes": 0,
                    "pipeline_created": False,
                    "agent_messages_published": False,
                },
            }

        batch = None
        if pipeline["assessment_batch_id"] is not None:
            batch = conn.execute(
                """
                SELECT candidate_start_sequence, candidate_end_sequence,
                       student_sequences_json
                FROM state_assessment_batches
                WHERE id=?
                """,
                (int(pipeline["assessment_batch_id"]),),
            ).fetchone()

        events = conn.execute(
            """
            SELECT event, details_json, occurred_at
            FROM strategy_pipeline_latency_events
            WHERE pipeline_run_id=?
            ORDER BY id
            """,
            (int(pipeline_id),),
        ).fetchall()

        stored_response = pipeline["state_raw_response_json"]
        finish_reason = None
        envelope = _json_value(stored_response)
        if isinstance(envelope, dict) and envelope.get("choices"):
            finish_reason = (
                (envelope.get("choices") or [{}])[0].get("finish_reason")
            )

        report = replay_stage2_response(
            stored_response,
            candidate_sequences=_candidate_sequences(batch),
            initial_finish_reason=finish_reason,
        )
        report["pipeline_run_id"] = int(pipeline["id"])
        report["pipeline"] = {
            "stage2_status": pipeline["stage2_status"],
            "failure_code": pipeline["failure_code"],
            "stage2_completed_at": pipeline["stage2_completed_at"],
            "stored_response_available": stored_response not in (None, ""),
        }
        report["audit_evidence"] = _safe_event_evidence(events)
        if report["final"]["status"] == "failed" and pipeline["failure_code"]:
            report["final"]["failure_category"] = (
                report["final"].get("failure_category")
                or pipeline["failure_code"]
            )
            report["final"]["evidence"] = {
                "pipeline_failure_code": pipeline["failure_code"],
                "stored_repair_response": False,
            }
        return report
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-id", type=int, default=831)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "ssrl_esp.db",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            replay_pipeline(args.db, args.pipeline_id),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
