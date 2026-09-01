# -*- coding: utf-8 -*-
"""
Huey tasks for student-help-request processing.

Each task pushes its own Flask application context and creates an
independent database session.  Idempotent by design: the task checks
status and intervention_run_id before any write operation.
"""
import logging
from huey_instance import huey
from core import app

logger = logging.getLogger(__name__)


def _schedule_help_lock_release(group_id: int, lock_token: str):
    try:
        from config import INTERVENTION_V2_LOCK_SECONDS
        from agent.intervention_tasks import release_expired_intervention
        release_expired_intervention.schedule(
            args=(group_id, lock_token),
            delay=INTERVENTION_V2_LOCK_SECONDS,
            priority=10,
        )
    except Exception as exc:
        logger.warning("Failed to schedule help lock release: %s", exc)


def _acquire_help_room_lock(group_id: int, help_request_id: int):
    try:
        from services.three_stage_coordination import preempt_for_student_help
        from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService

        preempt_for_student_help(help_request_id)
        lock_token = RoomLeaseService.acquire(group_id, -int(help_request_id))
        if lock_token:
            _schedule_help_lock_release(group_id, lock_token)
        return lock_token
    except Exception as exc:
        logger.warning("Failed to acquire help room lock for request %s: %s", help_request_id, exc)
        return None


def _release_help_room_lock(group_id: int, lock_token: str):
    if not lock_token:
        return
    try:
        from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService
        RoomLeaseService.release(group_id, lock_token)
    except Exception as exc:
        logger.warning("Failed to release help room lock for group %s: %s", group_id, exc)


def _release_help_room_lock_for_request(help_request_id: int):
    try:
        from db import query_one
        from services.intervention_pipeline_v2.room_lease_service import RoomLeaseService

        row = query_one("SELECT group_id FROM help_requests WHERE id=?", (help_request_id,))
        if not row:
            return
        room = query_one(
            """SELECT state, lock_token, active_intervention_run_id
               FROM groups WHERE id=?""",
            (row["group_id"],),
        )
        if not room or room["state"] != RoomLeaseService.LOCK_STATE:
            return
        if room["active_intervention_run_id"] != -int(help_request_id):
            return
        _release_help_room_lock(row["group_id"], room["lock_token"])
    except Exception as exc:
        logger.warning("Failed to release failed help lock for request %s: %s", help_request_id, exc)


def _linked_state_assessment_for_help(help_request: dict) -> dict:
    try:
        from db import query_one

        source_message_id = help_request.get("source_message_id")
        if not source_message_id:
            return {}
        msg = query_one(
            "SELECT sequence FROM messages WHERE id=? AND group_id=?",
            (source_message_id, help_request.get("group_id")),
        )
        if not msg or msg["sequence"] is None:
            return {}
        row = query_one(
            """
            SELECT id, state_assessment_id
              FROM monitor_runs
               WHERE group_id=?
                 AND (? IS NULL OR session_id=?)
                 AND (? IS NULL OR discussion_id=?)
                 AND (? IS NULL OR task_id=?)
                 AND state_assessment_id IS NOT NULL
                 AND cutoff_sequence>=?
             ORDER BY cutoff_sequence ASC, id ASC
             LIMIT 1
            """,
            (
                help_request.get("group_id"),
                  help_request.get("session_id"),
                  help_request.get("session_id"),
                  help_request.get("discussion_id"),
                  help_request.get("discussion_id"),
                  help_request.get("task_id"),
                  help_request.get("task_id"),
                  msg["sequence"],
            ),
        )
        return {
            "monitor_run_id": row["id"],
            "state_assessment_id": row["state_assessment_id"],
        } if row else {}
    except Exception as exc:
        logger.warning("Failed to resolve state assessment for help request %s: %s", help_request.get("id"), exc)
        return {}


@huey.task(retries=2, retry_delay=60, priority=100)
def process_student_help(help_request_id):


    """Process a student help request asynchronously.

    Flow: QUEUED -> RUNNING -> (COMPLETED | COMPLETED_WITH_FALLBACK | FAILED)

    Idempotency: checks help_requests.intervention_run_id and
    agent_suggestions.help_request_id UNIQUE constraint to prevent
    duplicate assistant messages on retry.
    """
    with app.app_context():
        try:
            _execute_help_flow(help_request_id)
        except Exception as exc:
            logger.error(
                "process_student_help(%s) unhandled error: %s",
                help_request_id, exc, exc_info=True,
            )
            from db import execute, now_str
            try:
                _release_help_room_lock_for_request(help_request_id)
                execute(
                    "UPDATE help_requests SET status='FAILED', handling_status='failed', failure_reason=?, handled_at=?, completed_at=? WHERE id=?",
                    (str(exc)[:200], now_str(), now_str(), help_request_id),
                )
            except Exception:
                pass


def _execute_help_flow(help_request_id):


    """Inner flow logic isolated for readability and error handling."""
    from db import db, execute, query_one, now_str
    from auth import get_group_condition

    # 1. Load help request
    row = query_one("SELECT * FROM help_requests WHERE id=?", (help_request_id,))
    if not row:
        logger.warning("help_request %s not found", help_request_id)
        return
    hr = dict(row)

    # 2. Idempotency: skip if already processed
    if str(hr["status"]).upper() in ("COMPLETED", "COMPLETED_WITH_FALLBACK"):
        logger.info("help_request %s already %s, skipping", help_request_id, hr["status"])
        return
    if hr.get("intervention_run_id") is not None:
        logger.info("help_request %s already has intervention_run_id, skipping", help_request_id)
        return
    if hr.get("response_message_id") is not None:
        logger.info("help_request %s already has response_message_id, skipping", help_request_id)
        return
    if hr.get("response_message"):
        logger.info("help_request %s already has response_message, skipping", help_request_id)
        return

    # 3. Atomically claim the task
    conn = db()
    cur = conn.execute(
        """UPDATE help_requests
              SET status='RUNNING', handling_status='running',
                  help_request_message_sequence=COALESCE(
                      help_request_message_sequence,
                      (SELECT sequence FROM messages WHERE id=help_requests.source_message_id)
                  )
            WHERE id=?
              AND intervention_run_id IS NULL
              AND response_message_id IS NULL
              AND UPPER(COALESCE(status, '')) IN ('QUEUED','PENDING')""",
        (help_request_id,),
    )
    affected = cur.rowcount
    conn.commit()
    conn.close()

    if affected == 0:
        logger.info("help_request %s claimed by another worker, skipping", help_request_id)
        return

    # 4. Build context and generate response (no long-held DB transaction)
    from services.student_help_service import (
        _recent_student_context, _detect_help_intent,
        _normalized_request_text, _build_help_message,
        _state_snapshot, _insert_student_help_suggestion,
        HELP_STRATEGY_LIBRARY, STUDENT_HELP_PUSH_MODE,
        STUDENT_HELP_TRIGGER_SOURCE, STUDENT_HELP_ANALYSIS_MODE,
        STUDENT_HELP_STRATEGY_VERSION,
    )
    from agent.detector import get_active_task, summarize_context
    from db import get_learning_task
    from agent.strategy import clean_student_visible_message
    from services.context_service import collect_group_context
    from services.agent_intervention_publisher import publish_agent_intervention

    group_id = hr["group_id"]
    request_text = hr.get("request_text") or ""
    normalized = _normalized_request_text(request_text)
    lock_token = _acquire_help_room_lock(group_id, help_request_id)
    if not lock_token:
        execute(
            "UPDATE help_requests SET status='FAILED', handling_status='failed', failure_reason='room_ai_intervening', handled_at=?, completed_at=? WHERE id=?",
            (now_str(), now_str(), help_request_id),
        )
        logger.info("help_request %s skipped because the room is already AI_INTERVENING", help_request_id)
        return

    # 4a. Build discussion context
    context_rows = _recent_student_context(
        group_id,
        limit=8,
        session_id=hr.get("session_id"),
        discussion_id=hr.get("discussion_id"),
    )
    task = (
        get_learning_task(int(hr["task_id"]))
        if hr.get("task_id") is not None
        else None
    ) or get_active_task() or {}
    intent = _detect_help_intent(normalized)
    strategy_meta = dict(HELP_STRATEGY_LIBRARY[intent])
    condition = get_group_condition(group_id)

    # 4b. LLM call (outside DB transaction)
    response_message = None
    llm_generated = False
    fallback_used = False

    from config import SERA_LLM_ENABLED
    print(f"[SERA DEBUG][help_tasks] SERA_LLM_ENABLED={SERA_LLM_ENABLED}, group={group_id}, intent={intent}, context_rows={len(context_rows)}")
    if SERA_LLM_ENABLED:
        try:
            from services.llm_analyzer import generate_student_help_response

            help_context = collect_group_context(
                group_id,
                task_id=hr.get("task_id"),
                session_no=hr.get("session_no"),
                session_id=hr.get("session_id"),
                discussion_id=hr.get("discussion_id"),
            )
            print(f"[SERA DEBUG][help_tasks] help_context collected: task={bool(help_context.get('current_task'))}, msgs={len(help_context.get('recent_student_messages') or [])}")
            if not help_context.get("current_task"):
                help_context["current_task"] = task

            template_guidance = _build_help_message(intent, task, summarize_context(context_rows), condition)
            strategy_meta_with_guidance = dict(strategy_meta)
            strategy_meta_with_guidance["template_guidance"] = template_guidance

            print(f"[SERA DEBUG][help_tasks] calling generate_student_help_response for group={group_id}...")
            llm_msg = generate_student_help_response(
                group_id, help_context, strategy_meta_with_guidance,
                condition, context_rows, normalized,
            )
            print(f"[SERA DEBUG][help_tasks] generate_student_help_response returned: {'YES' if llm_msg else 'None/empty'}")
            if llm_msg:
                response_message = clean_student_visible_message(llm_msg)
                llm_generated = True
                print(f"[SERA DEBUG][help_tasks] LLM success, message={repr(response_message[:100])}")
            else:
                print(f"[SERA DEBUG][help_tasks] LLM returned None, will use fallback")
        except Exception as exc:
            print(f"[SERA DEBUG][help_tasks] EXCEPTION during LLM call: {exc}")
            logger.warning("process_student_help LLM call failed [%s]: %s", help_request_id, exc)

    # 4c. Fall back to template message if LLM didn't produce one
    if not response_message:
        response_message = _build_help_message(intent, task, summarize_context(context_rows), condition)
        fallback_used = True
        print(f"[SERA DEBUG][help_tasks] FALLBACK used, message={repr(response_message[:100])}")
    else:
        print(f"[SERA DEBUG][help_tasks] Using LLM-generated message")

    # 5. Write results (short DB transactions)
    state = _state_snapshot(group_id, session_id=hr.get("session_id"))
    analysis_mode = "llm_context" if llm_generated else STUDENT_HELP_ANALYSIS_MODE
    print(f"[SERA DEBUG][help_tasks] analysis_mode={analysis_mode}, fallback_used={fallback_used}")

    suggestion = _insert_student_help_suggestion(
        group_id, state, strategy_meta, normalized, context_rows,
        response_message, analysis_mode=analysis_mode,
        help_request_id=help_request_id,
    )
    if not suggestion:
        logger.error("help_request %s: suggestion creation failed", help_request_id)
        execute(
            "UPDATE help_requests SET status='FAILED', handling_status='failed', failure_reason='suggestion_creation_failed', handled_at=?, completed_at=? WHERE id=?",
            (now_str(), now_str(), help_request_id),
        )
        _release_help_room_lock(group_id, lock_token)
        return

    try:
        linked_state = _linked_state_assessment_for_help(hr)
        publish_result = publish_agent_intervention(
            group_id=group_id,
            message=response_message,
            agent_type="strategy",
            trigger_source=STUDENT_HELP_TRIGGER_SOURCE,
            help_request_id=help_request_id,
            source_student_message_id=hr.get("source_message_id"),
            session_id=hr.get("session_id"),
            task_id=hr.get("task_id"),
            session_no=hr.get("session_no"),
            monitor_run_id=linked_state.get("monitor_run_id"),
            state_assessment_id=linked_state.get("state_assessment_id"),
            strategy_id=strategy_meta.get("strategy_id"),
            title=strategy_meta.get("strategy_name"),
            prompt_version=None,
            fallback_used=fallback_used,
            condition=condition,
            push_mode=STUDENT_HELP_PUSH_MODE,
            suggestion_id=suggestion.get("id"),
            template_id=strategy_meta.get("template_id"),
            sub_category=strategy_meta.get("strategy_name"),
            strategy_type="student_help",
            strategy_version=STUDENT_HELP_STRATEGY_VERSION,
            detected_state=state.get("state_code"),
            confidence=state.get("confidence"),
            lock_token=lock_token,
            help_intent=intent,
            metadata={
                "analysis_mode": analysis_mode,
                "fallback_used": fallback_used,
                "source_student_message_id": hr.get("source_message_id"),
            },
        )
        if not publish_result.get("ok"):
            logger.error("help_request %s: intervention execution failed", help_request_id)
            execute(
                "UPDATE help_requests SET status='FAILED', handling_status='failed', failure_reason='intervention_execution_failed', handled_at=?, completed_at=? WHERE id=?",
                (now_str(), now_str(), help_request_id),
            )
            _release_help_room_lock(group_id, lock_token)
            return
        logger.info(
            "help_request %s -> %s (fallback=%s run=%s message=%s)",
            help_request_id,
            publish_result.get("status"),
            fallback_used,
            publish_result.get("intervention_run_id"),
            publish_result.get("message_id"),
        )
        _release_help_room_lock(group_id, lock_token)
    except Exception as exc:
        logger.error("help_request %s: write phase failed: %s", help_request_id, exc)
        execute(
            "UPDATE help_requests SET status='FAILED', handling_status='failed', failure_reason=?, handled_at=?, completed_at=? WHERE id=?",
            (str(exc)[:200], now_str(), now_str(), help_request_id),
        )
        _release_help_room_lock(group_id, lock_token)
        raise

@huey.task()


def help_smoke():


    """Smoke test 鈥?verifies the help_tasks module is importable
    and tasks can execute under the Flask application context."""
    with app.app_context():
         return "help_smoke ok"


@huey.task()


def check_help_db():


    """Quick connectivity check against the business database.
    Opens and closes its own connection 鈥?no request session is reused."""
    with app.app_context():
         from db import db
         conn = db()
         try:
             conn.execute("SELECT 1").fetchone()
             conn.commit()
         finally:
             conn.close()
         return {"module": "help", "db_ok": True}


