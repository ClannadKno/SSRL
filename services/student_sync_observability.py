# -*- coding: utf-8 -*-
"""Baseline request metrics for the student sync refactor batches."""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from copy import deepcopy
from typing import Optional

from flask import g, request

logger = logging.getLogger("student_sync.baseline")


_TRACKED_ROUTES = {
    ("GET", "/api/group/<int:group_id>/messages"): "group_messages_poll",
    ("GET", "/api/rooms/<int:room_id>/events"): "room_events_poll",
    ("POST", "/api/heartbeat"): "student_heartbeat",
    ("GET", "/api/student/sync"): "student_sync",
    ("GET", "/api/collaborative-documents/current"): "collaborative_document_current",
    ("POST", "/login"): "login_key_lookup",
}

_LOCK = threading.Lock()
_METRICS = defaultdict(lambda: {"count": 0, "total_ms": 0.0, "max_ms": 0.0})


def _ensure_logger_ready() -> None:
    """Make metric logs visible even when the app has no logging config."""
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _route_rule() -> str:
    rule = getattr(request, "url_rule", None)
    return getattr(rule, "rule", None) or request.path


def _metric_name() -> Optional[str]:
    return _TRACKED_ROUTES.get((request.method.upper(), _route_rule()))


def _record(metric: str, route: str, method: str, status_code: int, elapsed_ms: float) -> None:
    with _LOCK:
        item = _METRICS[metric]
        item["count"] += 1
        item["total_ms"] += elapsed_ms
        item["max_ms"] = max(item["max_ms"], elapsed_ms)
        count = item["count"]
        avg_ms = item["total_ms"] / count
        max_ms = item["max_ms"]

    logger.info(
        "student_sync_baseline metric=%s method=%s route=%s status=%s "
        "elapsed_ms=%.2f count=%s avg_ms=%.2f max_ms=%.2f",
        metric,
        method,
        route,
        status_code,
        elapsed_ms,
        count,
        avg_ms,
        max_ms,
    )


def snapshot_student_sync_baseline_metrics() -> dict:
    """Return in-process counters for tests and local diagnostics."""
    with _LOCK:
        return deepcopy(dict(_METRICS))


def register_student_sync_baseline_metrics(app) -> None:
    """Register non-behavioral request timing/count logging for Batch 0."""
    if app.config.get("STUDENT_SYNC_BASELINE_METRICS_REGISTERED"):
        return
    app.config["STUDENT_SYNC_BASELINE_METRICS_REGISTERED"] = True
    _ensure_logger_ready()

    @app.before_request
    def _student_sync_baseline_start():
        g.student_sync_baseline_started_at = time.perf_counter()

    @app.after_request
    def _student_sync_baseline_finish(response):
        metric = _metric_name()
        if not metric:
            return response
        started_at = getattr(g, "student_sync_baseline_started_at", None)
        elapsed_ms = 0.0
        if started_at is not None:
            elapsed_ms = max(0.0, (time.perf_counter() - started_at) * 1000.0)
        _record(metric, _route_rule(), request.method.upper(), response.status_code, elapsed_ms)
        return response
