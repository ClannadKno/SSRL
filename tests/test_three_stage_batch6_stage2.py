# -*- coding: utf-8 -*-
"""Batch 6 coverage for Stage 2 extraction and read-only replay."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace


def _valid_output():
    return {
        "sub_category": "confusion",
        "canonical_state": "confusion",
        "confidence": 0.82,
        "evidence_message_ids": [1, 2],
    }


def _context():
    return {
        "group_id": 31,
        "recent_student_messages": [
            {
                "id": 101,
                "sequence": 1,
                "role": "student",
                "user_id": 1,
                "content": "Which path should we take?",
            },
            {
                "id": 102,
                "sequence": 2,
                "role": "student",
                "user_id": 2,
                "content": "I am not sure which topic we are discussing.",
            },
        ],
    }


class _Result:
    def __init__(
        self,
        output=None,
        *,
        raw_text=None,
        success=True,
        failure_type=None,
        finish_reason="stop",
    ):
        self.success = success
        self.output = output
        self.raw_text = (
            raw_text
            if raw_text is not None
            else output
            if isinstance(output, str)
            else json.dumps(output, ensure_ascii=False)
            if output is not None
            else ""
        )
        self.model_name = "batch6-stage2-model"
        self.profile_name = "state_detector"
        self.latency_ms = 1
        self.attempt_count = 1
        self.token_usage = None
        self.failure_type = failure_type
        self.failure_message = failure_type
        self.finish_reason = finish_reason


class _Gateway:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.profiles = {
            "state_detector": SimpleNamespace(
                model="batch6-profile-model",
                read_timeout=45,
                retries=0,
            )
        }

    def call(self, profile, payload, response_type):
        self.calls.append((profile, payload, response_type))
        return self.result


def test_stage2_parser_extracts_fenced_json_with_prose_and_extra_fields():
    from services.discussion_pipeline_v2.llm_state_detector import parse_llm_json_content

    content = (
        "The compact result is below.\n"
        "```json\n"
        '{"confidence":0.82,"extra":"ignored",'
        '"evidence_message_ids":[1,2],"canonical_state":"confusion",'
        '"sub_category":"confusion"}\n'
        "```\n"
        "No further action is included."
    )

    parsed = parse_llm_json_content(content, candidate_sequences=[1, 2])

    assert parsed["valid"] is True
    assert parsed["data"]["state_recognition"]["canonical_state"] == "confusion"
    assert parsed["data"]["state_recognition"]["evidence_message_ids"] == [1, 2]


def test_stage2_parser_distinguishes_unterminated_json_from_invalid_json():
    from services.discussion_pipeline_v2.llm_state_detector import parse_llm_json_content

    truncated = parse_llm_json_content(
        "model preface {\"sub_category\":\"confusion\"",
        candidate_sequences=[1],
    )
    malformed = parse_llm_json_content(
        "model preface {\"sub_category\": confusion}",
        candidate_sequences=[1],
    )

    assert truncated["error_type"] == "truncated_response"
    assert malformed["error_type"] == "json_parse_error"


def test_stage2_accepts_complete_content_when_gateway_reports_length(monkeypatch):
    import services.discussion_pipeline_v2.llm_state_detector as detector

    content = json.dumps(_valid_output(), ensure_ascii=False)
    raw_envelope = json.dumps(
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "length",
                }
            ]
        },
        ensure_ascii=False,
    )
    gateway = _Gateway(
        _Result(
            raw_text=raw_envelope,
            success=False,
            failure_type="truncated_response",
            finish_reason="length",
        )
    )
    monkeypatch.setattr(detector, "SERA_LLM_ENABLED", True)
    monkeypatch.setattr(detector, "get_gateway", lambda: gateway)
    monkeypatch.setattr(detector, "record_latency_event", lambda **_kwargs: None)
    monkeypatch.setattr(detector, "safe_write_audit_log", lambda **_kwargs: True)

    envelope = detector.LLMStateDetector.detect(_context())

    assert len(gateway.calls) == 1
    assert envelope["meta"]["success"] is True
    assert envelope["meta"]["validation_status"] == "passed"
    attempt = envelope["meta"]["validation_attempts"][0]
    assert attempt["accepted_after_local_parse"] is True
    assert attempt["response_incomplete"] is True
    assert attempt["failure_category"] is None
    assert envelope["result"]["primary_state"] == "blocked_frustration"


def test_stage2_replay_is_bounded_and_has_no_side_effects():
    from services.discussion_pipeline_v2.llm_state_detector import replay_stage2_response

    replay = replay_stage2_response(
        "prefix {\"sub_category\":\"confusion\"",
        repair_response="```json\n" + json.dumps(_valid_output()) + "\n```",
        candidate_sequences=[1, 2],
        initial_finish_reason="length",
    )

    assert replay["read_only"] is True
    assert replay["external_call_count"] == 0
    assert replay["local_parse"]["valid"] is False
    assert replay["repair"]["status"] == "parsed"
    assert replay["repair"]["parse"]["valid"] is True
    assert replay["final"]["status"] == "success"
    assert replay["side_effects"] == {
        "database_writes": 0,
        "pipeline_created": False,
        "agent_messages_published": False,
    }


def test_pipeline_replay_reads_database_without_mutating_it(tmp_path: Path):
    from scripts.replay_stage2_pipeline import replay_pipeline

    database_path = tmp_path / "replay.db"
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        CREATE TABLE strategy_pipeline_runs (
            id INTEGER PRIMARY KEY,
            stage2_status TEXT,
            failure_code TEXT,
            stage2_completed_at TEXT,
            state_raw_response_json TEXT,
            assessment_batch_id INTEGER
        );
        CREATE TABLE state_assessment_batches (
            id INTEGER PRIMARY KEY,
            candidate_start_sequence INTEGER,
            candidate_end_sequence INTEGER,
            student_sequences_json TEXT
        );
        CREATE TABLE strategy_pipeline_latency_events (
            id INTEGER PRIMARY KEY,
            pipeline_run_id INTEGER,
            event TEXT,
            details_json TEXT,
            occurred_at TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO state_assessment_batches VALUES (1, 121, 122, '[121, 122]')"
    )
    connection.execute(
        "INSERT INTO strategy_pipeline_runs VALUES (?, ?, ?, ?, ?, ?)",
        (
            831,
            "FAILED",
            "truncated_response",
            "2026-08-01 02:36:39",
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {"content": ""},
                            "finish_reason": "length",
                        }
                    ]
                }
            ),
            1,
        ),
    )
    connection.execute(
        "INSERT INTO strategy_pipeline_latency_events VALUES (?, ?, ?, ?, ?)",
        (1, 831, "stage2_finished", '{"failure_category":"response_truncated"}', "now"),
    )
    connection.commit()
    before = connection.execute(
        "SELECT stage2_status, failure_code FROM strategy_pipeline_runs WHERE id=831"
    ).fetchone()
    connection.close()

    report = replay_pipeline(database_path, 831)

    connection = sqlite3.connect(database_path)
    after = connection.execute(
        "SELECT stage2_status, failure_code FROM strategy_pipeline_runs WHERE id=831"
    ).fetchone()
    connection.close()
    assert report["pipeline_run_id"] == 831
    assert report["final"]["status"] == "failed"
    assert report["final"]["failure_category"] == "response_truncated"
    assert report["side_effects"]["database_writes"] == 0
    assert after == before
