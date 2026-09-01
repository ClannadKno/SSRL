from __future__ import annotations


def test_recent_state_is_a_bounded_summary_without_recursive_audit_payloads(monkeypatch):
    from services import context_service

    row = {
        "id": 17,
        "group_id": 3,
        "session_id": 5,
        "session_no": 105,
        "discussion_id": 8,
        "task_id": 2,
        "state_assessment_id": 21,
        "state_code": "positive_collaboration",
        "state_label": "积极协作",
        "llm_state_code": "positive_collaboration",
        "assessment_status": "confirmed",
        "confirmation_status": "confirmed",
        "confirmed_windows": 2,
        "risk_level": 0,
        "risk_label": "低",
        "state_score": 0.8,
        "created_at": "2026-07-28 22:00:00",
        "context_json": {"recent_state": {"context_json": "x" * 200_000}},
        "feature_json": {"messages": "x" * 100_000},
        "rule_assessment_json": {"rules": "x" * 100_000},
        "fusion_json": {"fusion": "x" * 100_000},
        "evidence": "x" * 100_000,
    }
    captured = {}

    def fake_query_one(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return row

    monkeypatch.setattr(context_service, "query_one", fake_query_one)

    result = context_service._collect_recent_state(3, session_id=5, discussion_id=8)

    assert result["state_code"] == "positive_collaboration"
    assert result["assessment_status"] == "confirmed"
    assert result["session_id"] == 5
    assert result["discussion_id"] == 8
    assert "context_json" not in result
    assert "feature_json" not in result
    assert "rule_assessment_json" not in result
    assert "fusion_json" not in result
    assert "evidence" not in result
    assert "SELECT *" not in captured["sql"]
    assert captured["params"] == (3, 5, 8)
    assert len(str(result)) < 1_000
