# -*- coding: utf-8 -*-
"""Application entrypoint for SSRL-ESP."""
import os
from core import app
from config import APP_DEBUG, APP_HOST, APP_PORT
from db import ensure_database_ready
from services.scheduler_service import is_scheduler_running, start_scheduler, stop_scheduler
from services.student_sync_observability import register_student_sync_baseline_metrics
from startup_check import run_all_checks

import routes.api  # noqa: F401
import routes.collab_pages  # noqa: F401
import routes.export  # noqa: F401
import routes.pages  # noqa: F401
import routes.collaborative_api  # noqa: F401
import routes.teacher_api  # noqa: F401

register_student_sync_baseline_metrics(app)
ensure_database_ready()


def start_background_services():
    from config import DISCUSSION_PIPELINE_V2_ENABLED
    if DISCUSSION_PIPELINE_V2_ENABLED:
        print("[app] V2 pipeline enabled; V1 background scheduler disabled")
        print("[app] V2 uses Huey task queue for monitoring and intervention")
        print("[app] Make sure Huey consumer is running: huey_consumer huey_instance.huey -k thread -w 2")
        scheduler_started = False
    else:
        scheduler_started = start_scheduler()
        if scheduler_started:
            print("[app] V1 old pipeline scheduler started")
    return {
        "scheduler_started": bool(scheduler_started),
        "scheduler_running": bool(is_scheduler_running()),
    }


def stop_background_services(wait=True):
    scheduler_stopped = stop_scheduler(wait=wait)
    return {
        "scheduler_stopped": bool(scheduler_stopped),
        "scheduler_running": bool(is_scheduler_running()),
    }


if __name__ == "__main__":
    print("=" * 48)
    # [collab-diagnosis] Startup diagnostics
    import hashlib as _h_cd
    if os.environ.get("COLLAB_DIAG") == "1":
        from config import COLLAB_WS_HOST, COLLAB_WS_PORT, COLLAB_TOKEN_TTL
        _s = os.environ.get("SSRL_ESP_SECRET", "")
        _fp = _h_cd.sha256(_s.encode()).hexdigest()[:8] if _s else "NOT_SET"
        print(f"[collab-diagnosis] SSRL_ESP_SECRET fingerprint: {_fp}")
        print(f"[collab-diagnosis] COLLAB_WS_HOST: {COLLAB_WS_HOST}")
        print(f"[collab-diagnosis] COLLAB_WS_PORT: {COLLAB_WS_PORT}")
        print(f"[collab-diagnosis] COLLAB_TOKEN_TTL: {COLLAB_TOKEN_TTL}")
    print(f"[app] Server starting at http://{APP_HOST}:{APP_PORT}")
    #
    # Flask threading / reloader settings (env-var controlled):
    #   threaded=True      -- support concurrent HTTP requests in dev/local mode
    #   use_reloader=False -- avoid re-initialising secrets & background processes
    #   Production deployments should replace with Waitress (subsequent batch).
    #
    FLASK_THREADED = os.environ.get("FLASK_THREADED", "true").strip().lower() in {"1", "true", "yes", "y", "on"}
    FLASK_USE_RELOADER = os.environ.get("FLASK_USE_RELOADER", "false").strip().lower() in {"1", "true", "yes", "y", "on"}

    app.run(
        host=APP_HOST,
        port=APP_PORT,
        debug=APP_DEBUG,
        threaded=FLASK_THREADED,
        use_reloader=FLASK_USE_RELOADER,
    )
