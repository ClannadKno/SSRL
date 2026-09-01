# -*- coding: utf-8 -*-
"""Live HTTP concurrency probes for 60 students across 15 groups."""

from __future__ import annotations

import json
import http.client
import builtins
import statistics
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from waitress import create_server

from tests.conftest import purge_modules


GROUP_COUNT = 15
USERS_PER_GROUP = 4
TOTAL_USERS = GROUP_COUNT * USERS_PER_GROUP
MOCK_LLM_SECONDS = 0.15
WAITRESS_THREADS = 8
HUEY_WORKERS = 2


@pytest.fixture(autouse=True)
def block_network():
    """Override the suite-wide network blocker; this test needs localhost HTTP."""
    yield


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95 + 0.999) - 1))
    return round(ordered[idx], 1)


def _safe_json(status: int, text: str) -> dict:
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {"payload": payload}
    except Exception:
        return {"text": text[:300], "status": status}


def _summary(label: str, results: list[dict], elapsed_ms: float) -> dict:
    latencies = [item["latency_ms"] for item in results if item.get("latency_ms") is not None]
    statuses = Counter(str(item.get("status")) for item in results)
    errors = [item.get("error") for item in results if item.get("error")]
    by_kind = {}
    for kind in sorted({item.get("kind", "unknown") for item in results}):
        kind_items = [item for item in results if item.get("kind", "unknown") == kind]
        kind_latencies = [item["latency_ms"] for item in kind_items if item.get("latency_ms") is not None]
        by_kind[kind] = {
            "count": len(kind_items),
            "ok": sum(1 for item in kind_items if item.get("ok")),
            "median_ms": round(statistics.median(kind_latencies), 1) if kind_latencies else None,
            "p95_ms": _p95(kind_latencies),
            "max_ms": round(max(kind_latencies), 1) if kind_latencies else None,
        }
    summary = {
        "label": label,
        "count": len(results),
        "elapsed_ms": round(elapsed_ms, 1),
        "ok": sum(1 for item in results if item.get("ok")),
        "statuses": dict(statuses),
        "errors": errors[:5],
        "median_ms": round(statistics.median(latencies), 1) if latencies else None,
        "p95_ms": _p95(latencies),
        "max_ms": round(max(latencies), 1) if latencies else None,
        "by_kind": by_kind,
    }
    print("CONCURRENCY_METRIC " + json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return summary


def _run_actions(label: str, actions, max_workers: int) -> tuple[list[dict], dict]:
    started = time.perf_counter()
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(action) for action in actions]
        for future in as_completed(futures):
            results.append(future.result())
    elapsed_ms = (time.perf_counter() - started) * 1000
    return results, _summary(label, results, elapsed_ms)


@pytest.fixture()
def live_concurrency_app(tmp_path, monkeypatch):
    env = {
        "SSRL_ESP_DB_PATH": str(tmp_path / "concurrency.db"),
        "HUEY_DB_PATH": str(tmp_path / "huey.db"),
        "SSRL_ESP_UPLOAD_DIR": str(tmp_path / "uploads"),
        "SSRL_ESP_SECRET": "concurrency-test-secret",
        "EXPERIMENT_MODE": "0",
        "RESET_DEMO_PASSWORDS_ON_START": "0",
        "USE_LLM_ANALYSIS": "1",
        "SERA_LLM_ENABLED": "1",
        "SERA_LLM_API_KEY": "test-key",
        "SERA_LLM_BASE_URL": "https://llm-test.invalid/v1/chat/completions",
        "SERA_LLM_MODEL": "gpt-test",
        "HUEY_ENABLED": "1",
        "HUEY_IMMEDIATE": "0",
        "DISCUSSION_PIPELINE_V2_ENABLED": "1",
        "DISCUSSION_PIPELINE_V2_SHADOW": "0",
        "AUTO_INTERVENTION_V2_ENABLED": "0",
        "PIPELINE_V2_MIN_NEW_MESSAGES_FOR_LLM": "1",
        "PIPELINE_V2_LLM_COOLDOWN_SECONDS": "0",
        "STUDENT_HELP_COOLDOWN_SECONDS": "0",
        "STUDENT_HELP_MAX_REQUESTS_PER_WINDOW": "999",
        "STUDENT_HELP_WINDOW_MINUTES": "10",
        "ENABLE_BACKGROUND_SCHEDULER": "0",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    original_print = builtins.print

    def filtered_print(*args, **kwargs):
        if args and str(args[0]).startswith("CONCURRENCY_"):
            original_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", filtered_print)

    purge_modules()
    import importlib
    print("CONCURRENCY_STAGE importing_app", flush=True)

    db = importlib.import_module("db")
    db.ensure_database_ready()
    app_module = importlib.import_module("app")
    print("CONCURRENCY_STAGE app_ready", flush=True)

    now = db.now_str()
    task_id = db.execute(
        "INSERT INTO learning_tasks(title, question, time_limit_minutes, created_at) VALUES(?,?,?,?)",
        ("Concurrency task", "Discuss and coordinate under load", 60, now),
    )
    session_id = db.execute(
        """
        INSERT INTO experiment_sessions(
            session_no, session_role, task_id, status, start_time,
            time_limit_minutes, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (1, "discussion", task_id, "running", now, 60, now, now),
    )
    db.set_setting("current_session_id", str(session_id))
    db.set_setting("current_session_no", "1")
    db.set_setting("current_task_id", str(task_id))

    accounts = []
    for group_no in range(1, GROUP_COUNT + 1):
        group_id = db.execute(
            "INSERT INTO groups(name, group_code, condition, state, created_at) VALUES(?,?,?,?,?)",
            (f"Concurrency Group {group_no:02d}", f"CG{group_no:02d}", "experiment", "OPEN", now),
        )
        for member_no in range(1, USERS_PER_GROUP + 1):
            participant_code = f"CG{group_no:02d}-M{member_no}"
            username = f"concurrency_g{group_no:02d}_m{member_no}"
            user_id = db.execute(
                """
                INSERT INTO users(username, password_hash, real_name, participant_code, role, created_at)
                VALUES(?,?,?,?,?,?)
                """,
                (username, "x", f"Student {group_no:02d}-{member_no}", participant_code, "student", now),
            )
            db.execute("INSERT INTO group_members(group_id, user_id) VALUES(?,?)", (group_id, user_id))
            db.execute(
                """
                INSERT INTO experiment_participants(
                    participant_code, login_key_hash, group_no, member_no, group_id,
                    user_id, display_name, is_active, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    participant_code,
                    "unused-in-concurrency-test",
                    group_no,
                    member_no,
                    group_id,
                    user_id,
                    participant_code,
                    1,
                    now,
                ),
            )
            accounts.append(
                {
                    "group_no": group_no,
                    "group_id": group_id,
                    "member_no": member_no,
                    "user_id": user_id,
                    "participant_code": participant_code,
                }
            )

    conn = db.db()
    try:
        for account in accounts:
            token = uuid.uuid4().hex
            account["tab_token"] = token
            conn.execute(
                """
                INSERT INTO client_sessions(token, user_id, role, login_method, created_at, last_seen)
                VALUES(?,?,?,?,?,?)
                """,
                (token, account["user_id"], "student", "participant_key", now, now),
            )
        conn.commit()
    finally:
        conn.close()
    print("CONCURRENCY_STAGE seed_ready", flush=True)

    import services.llm_analyzer as llm_analyzer
    import services.discussion_pipeline_v2.llm_state_detector as llm_state_detector
    import services.discussion_pipeline_v2.trigger_policy as trigger_policy

    def fake_student_help(group_id, context, strategy_meta, condition, context_rows, help_request_text):
        time.sleep(MOCK_LLM_SECONDS)
        return f"Group {group_id}: choose one next step and assign a recorder."

    def fake_state_detect(context):
        time.sleep(MOCK_LLM_SECONDS)
        return {
            "result": {
                "state_code": "coordination_needed",
                "confidence": 0.6,
                "reason": "load-test synthetic state",
            },
            "meta": {
                "analysis_skipped": False,
                "profile": "state_detector",
                "model_name": "fake",
                "prompt_version": "load-test",
                "latency_ms": round(MOCK_LLM_SECONDS * 1000, 1),
                "success": True,
            },
        }

    monkeypatch.setattr(llm_analyzer, "generate_student_help_response", fake_student_help)
    monkeypatch.setattr(llm_state_detector.LLMStateDetector, "detect", staticmethod(fake_state_detect))
    monkeypatch.setattr(
        trigger_policy.TriggerPolicy,
        "should_call_llm",
        staticmethod(lambda group_id, cutoff_sequence, rule_assessment: (True, "load_test_force_llm")),
    )
    print("CONCURRENCY_STAGE llm_patched", flush=True)

    server = create_server(app_module.app, host="127.0.0.1", port=0, threads=WAITRESS_THREADS)
    print(f"CONCURRENCY_STAGE server_created port={server.effective_port}", flush=True)
    thread = threading.Thread(target=server.run, name="waitress-concurrency-test", daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.effective_port}"
    print("CONCURRENCY_STAGE server_thread_started", flush=True)

    for _ in range(50):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.effective_port, timeout=5)
            conn.request("GET", "/login")
            response = conn.getresponse()
            status = response.status
            response.read()
            conn.close()
            if status in (200, 302, 405):
                print(f"CONCURRENCY_STAGE probe_ok status={status}", flush=True)
                break
        except Exception:
            time.sleep(0.05)
    else:
        server.close()
        raise RuntimeError("Waitress test server did not start")

    try:
        serializer = app_module.app.session_interface.get_signing_serializer(app_module.app)
        cookie_name = app_module.app.config.get("SESSION_COOKIE_NAME", "session")
        for account in accounts:
            session_cookie = serializer.dumps(
                {
                    "user_id": account["user_id"],
                    "role": "student",
                    "participant_code": account["participant_code"],
                    "group_id": account["group_id"],
                    "display_name": account["participant_code"],
                    "login_method": "participant_key",
                }
            )
            account["host"] = "127.0.0.1"
            account["port"] = server.effective_port
            account["headers"] = {
                "X-Tab-Token": account["tab_token"],
                "Cookie": f"{cookie_name}={session_cookie}",
            }
        print("CONCURRENCY_STAGE clients_ready", flush=True)

        yield db, accounts, base_url
    finally:
        server.close()
        thread.join(timeout=5)
        purge_modules()


def _post_json(account: dict, path: str, payload: dict) -> tuple[int, dict, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Content-Length": str(len(body)),
        **account["headers"],
    }
    conn = http.client.HTTPConnection(account["host"], account["port"], timeout=30)
    try:
        conn.request("POST", path, body=body, headers=headers)
        response = conn.getresponse()
        text = response.read().decode("utf-8", errors="replace")
        return response.status, _safe_json(response.status, text), text
    finally:
        conn.close()


def _post_help(account: dict) -> dict:
    started = time.perf_counter()
    try:
        status, data, _text = _post_json(
            account,
            "/api/student/help",
            {
                "group_id": account["group_id"],
                "request_text": f"member {account['member_no']} needs a next step",
                "client_message_id": f"help-{time.perf_counter_ns()}",
            },
        )
        return {
            "kind": "help",
            "group_id": account["group_id"],
            "status": status,
            "ok": status == 202 and data.get("accepted") is True,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "error": data.get("error") or data.get("reason") or data.get("text"),
        }
    except Exception as exc:  # pragma: no cover - diagnostic surface
        return {
            "kind": "help",
            "group_id": account["group_id"],
            "status": "exception",
            "ok": False,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _post_state_message(account: dict) -> dict:
    started = time.perf_counter()
    try:
        status, data, _text = _post_json(
            account,
            "/api/message",
            {
                "group_id": account["group_id"],
                "content": f"state trigger from member {account['member_no']} at {time.perf_counter_ns()}",
                "client_message_id": f"msg-{time.perf_counter_ns()}",
            },
        )
        return {
            "kind": "state_message",
            "group_id": account["group_id"],
            "status": status,
            "ok": status == 200 and data.get("ok") is True,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "error": data.get("error") or data.get("text"),
        }
    except Exception as exc:  # pragma: no cover - diagnostic surface
        return {
            "kind": "state_message",
            "group_id": account["group_id"],
            "status": "exception",
            "ok": False,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _run_detection_for_group(group_id: int) -> dict:
    from services.discussion_pipeline_v2.monitoring_service import MonitoringService

    started = time.perf_counter()
    try:
        result = MonitoringService.run_detection(group_id, trigger_type="load_test")
        return {
            "kind": "state_worker",
            "group_id": group_id,
            "status": result.get("reason") or ("error" if result.get("error") else "ok"),
            "ok": not bool(result.get("error")),
            "latency_ms": (time.perf_counter() - started) * 1000,
            "error": result.get("error"),
        }
    except Exception as exc:  # pragma: no cover - diagnostic surface
        return {
            "kind": "state_worker",
            "group_id": group_id,
            "status": "exception",
            "ok": False,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "error": f"{type(exc).__name__}: {exc}",
        }


def test_60_students_15_groups_help_state_and_mixed_concurrency(live_concurrency_app):
    db, accounts, _base_url = live_concurrency_app
    assert len(accounts) == TOTAL_USERS

    help_results, help_summary = _run_actions(
        "all_help_http_60_waitress8",
        [lambda account=account: _post_help(account) for account in accounts],
        max_workers=TOTAL_USERS,
    )
    assert len(help_results) == TOTAL_USERS
    assert all(item["ok"] for item in help_results), help_summary

    state_results, state_summary = _run_actions(
        "all_state_message_http_60_waitress8",
        [lambda account=account: _post_state_message(account) for account in accounts],
        max_workers=TOTAL_USERS,
    )
    assert len(state_results) == TOTAL_USERS
    assert all(item["ok"] for item in state_results), state_summary

    mixed_actions = []
    for idx, account in enumerate(accounts):
        if idx % 2 == 0:
            mixed_actions.append(lambda account=account: _post_help(account))
        else:
            mixed_actions.append(lambda account=account: _post_state_message(account))
    mixed_results, mixed_summary = _run_actions(
        "mixed_help30_state30_http_waitress8",
        mixed_actions,
        max_workers=TOTAL_USERS,
    )
    assert len(mixed_results) == TOTAL_USERS
    assert all(item["ok"] for item in mixed_results), mixed_summary

    group_ids = sorted({account["group_id"] for account in accounts})
    worker_results, worker_summary = _run_actions(
        "state_detection_worker_15_huey2",
        [lambda group_id=group_id: _run_detection_for_group(group_id) for group_id in group_ids],
        max_workers=HUEY_WORKERS,
    )
    assert len(worker_results) == GROUP_COUNT
    assert all(item["ok"] for item in worker_results), worker_summary

    open_locked = db.query_all(
        """
        SELECT id, state, lock_token
        FROM groups
        WHERE state <> 'OPEN' OR lock_token IS NOT NULL
        """
    )
    assert [dict(row) for row in open_locked] == []

    counts = {
        "messages": db.query_one("SELECT COUNT(*) AS c FROM messages")["c"],
        "help_requests": db.query_one("SELECT COUNT(*) AS c FROM help_requests")["c"],
        "intervention_logs": db.query_one("SELECT COUNT(*) AS c FROM intervention_logs")["c"],
        "monitor_runs": db.query_one("SELECT COUNT(*) AS c FROM monitor_runs")["c"],
    }
    print("CONCURRENCY_DB_COUNTS " + json.dumps(counts, ensure_ascii=False, sort_keys=True), flush=True)
    assert counts["help_requests"] >= 90
    assert counts["intervention_logs"] == 0
    assert counts["messages"] >= 180
    assert counts["monitor_runs"] >= GROUP_COUNT
