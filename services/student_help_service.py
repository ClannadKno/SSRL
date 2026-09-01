# -*- coding: utf-8 -*-
"""Student-initiated learning-assistant help flow for Phase 11."""
import re
from datetime import datetime, timedelta

from auth import get_group_condition
from agent.detector import get_active_task, latest_group_state, summarize_context
from config import (
    STUDENT_HELP_COOLDOWN_SECONDS,
    STUDENT_HELP_MAX_REQUESTS_PER_WINDOW,
    STUDENT_HELP_WINDOW_MINUTES,
)
from db import create_message, db, execute, now_str, parse_dt, query_all, query_one

STUDENT_HELP_TRIGGER_SOURCE = "student_help_request"
STUDENT_HELP_PUSH_MODE = "student_request"
STUDENT_HELP_SOURCE = "student_request"
STUDENT_HELP_ANALYSIS_MODE = "student_request"
STUDENT_HELP_STRATEGY_VERSION = "phase11_student_help_v1"
DEFAULT_HELP_REQUEST_TEXT = "请帮我们梳理下一步。"
HELP_TRIGGER_PATTERN = re.compile(r"^\s*[@＠]\s*(?:学习助手|sera)(?:\s*[:：,，-]\s*|\s+)?", re.IGNORECASE)

HELP_STRATEGY_LIBRARY = {
    "task_explanation": {
        "strategy_id": "HELP_EXPLAIN_TASK",
        "template_id": "HELP-REQ-01",
        "strategy_name": "解释任务要求",
    },
    "discussion_framework": {
        "strategy_id": "HELP_DISCUSSION_FRAMEWORK",
        "template_id": "HELP-REQ-02",
        "strategy_name": "提供讨论框架",
    },
    "compare_views": {
        "strategy_id": "HELP_COMPARE_VIEWS",
        "template_id": "HELP-REQ-03",
        "strategy_name": "帮助比较观点",
    },
    "summarize_progress": {
        "strategy_id": "HELP_SUMMARIZE_PROGRESS",
        "template_id": "HELP-REQ-04",
        "strategy_name": "帮助总结进度",
    },
    "limited_knowledge": {
        "strategy_id": "HELP_LIMITED_KNOWLEDGE_CUE",
        "template_id": "HELP-REQ-05",
        "strategy_name": "提供有限知识线索",
    },
    "next_step": {
        "strategy_id": "HELP_DEFINE_NEXT_STEP",
        "template_id": "HELP-REQ-06",
        "strategy_name": "提示下一步",
    },
}


def extract_student_help_request(content):
    text = (content or "").strip()
    if not text or not HELP_TRIGGER_PATTERN.match(text):
        if text and ("学习助手" in text or "sera" in text.lower()):
            print(f"[SERA DEBUG][extract] pattern NOT matched for text containing trigger word: {repr(text[:60])}")
        return None
    request_text = HELP_TRIGGER_PATTERN.sub("", text, count=1).strip()
    result = request_text or DEFAULT_HELP_REQUEST_TEXT
    print(f"[SERA DEBUG][extract] TRIGGER MATCHED: input={repr(text[:60])} -> extracted={repr(result[:60])}")
    return result



def _normalized_request_text(request_text):
    text = (request_text or "").strip()
    text = HELP_TRIGGER_PATTERN.sub("", text, count=1).strip()
    return text or DEFAULT_HELP_REQUEST_TEXT



def _student_help_message_text(request_text):
    return f"@学习助手 {_normalized_request_text(request_text)}"



def _scope_filter_sql(session_id=None, discussion_id=None, *, alias=""):
    prefix = f"{alias}." if alias else ""
    clauses = []
    params = []
    if session_id is not None:
        clauses.append(f"{prefix}session_id=?")
        params.append(int(session_id))
    if discussion_id is not None:
        clauses.append(f"{prefix}discussion_id=?")
        params.append(int(discussion_id))
    return clauses, params


def _recent_student_context(
    group_id,
    limit=8,
    *,
    session_id=None,
    discussion_id=None,
):
    scope_clauses, scope_params = _scope_filter_sql(
        session_id,
        discussion_id,
        alias="m",
    )
    scope_sql = "".join(f" AND {clause}" for clause in scope_clauses)
    rows = query_all(
        f"""
        SELECT m.*, u.real_name, u.username, u.role
        FROM messages m
        JOIN users u ON m.user_id = u.id
        WHERE m.group_id=? AND u.role='student'{scope_sql}
        ORDER BY m.id DESC
        LIMIT ?
        """,
        (group_id, *scope_params, limit),
    )
    return list(reversed(rows))



def _seconds_since(created_at):
    dt = parse_dt(created_at)
    if not dt:
        return None
    return max(0, int((datetime.now() - dt).total_seconds()))



def _latest_student_help_log(
    group_id,
    *,
    session_id=None,
    discussion_id=None,
):
    scope_clauses, scope_params = _scope_filter_sql(
        session_id,
        discussion_id,
    )
    scope_sql = "".join(f" AND {clause}" for clause in scope_clauses)
    row = query_one(
        f"""
        SELECT *
        FROM intervention_logs
        WHERE group_id=?
          AND (push_mode=? OR trigger_source=?)
          {scope_sql}
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            group_id,
            STUDENT_HELP_PUSH_MODE,
            STUDENT_HELP_TRIGGER_SOURCE,
            *scope_params,
        ),
    )
    return dict(row) if row else None



def _latest_student_help_request(
    group_id,
    *,
    session_id=None,
    discussion_id=None,
):
    scope_clauses, scope_params = _scope_filter_sql(
        session_id,
        discussion_id,
    )
    scope_sql = "".join(f" AND {clause}" for clause in scope_clauses)
    row = query_one(
        f"""
        SELECT *
        FROM help_requests
        WHERE group_id=?
          AND status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_FALLBACK')
          {scope_sql}
        ORDER BY id DESC
        LIMIT 1
        """,
        (group_id, *scope_params),
    )
    return dict(row) if row else None



def _latest_student_help_activity(
    group_id,
    *,
    session_id=None,
    discussion_id=None,
):
    candidates = [
        item for item in (
            _latest_student_help_log(
                group_id,
                session_id=session_id,
                discussion_id=discussion_id,
            ),
            _latest_student_help_request(
                group_id,
                session_id=session_id,
                discussion_id=discussion_id,
            ),
        )
        if item
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: parse_dt(item.get("created_at")) or datetime.min,
    )



def _count_recent_student_help_logs(
    group_id,
    *,
    session_id=None,
    discussion_id=None,
):
    since = (datetime.now() - timedelta(minutes=max(1, int(STUDENT_HELP_WINDOW_MINUTES)))).strftime("%Y-%m-%d %H:%M:%S")
    scope_clauses, scope_params = _scope_filter_sql(
        session_id,
        discussion_id,
    )
    scope_sql = "".join(f" AND {clause}" for clause in scope_clauses)
    row = query_one(
        f"""
        SELECT COUNT(*) AS c
        FROM intervention_logs
        WHERE group_id=?
          AND (push_mode=? OR trigger_source=?)
          AND created_at>=?
          {scope_sql}
        """,
        (
            group_id,
            STUDENT_HELP_PUSH_MODE,
            STUDENT_HELP_TRIGGER_SOURCE,
            since,
            *scope_params,
        ),
    )
    return int(row["c"] or 0) if row else 0



def _count_recent_student_help_requests(
    group_id,
    *,
    session_id=None,
    discussion_id=None,
):
    since = (datetime.now() - timedelta(minutes=max(1, int(STUDENT_HELP_WINDOW_MINUTES)))).strftime("%Y-%m-%d %H:%M:%S")
    scope_clauses, scope_params = _scope_filter_sql(
        session_id,
        discussion_id,
    )
    scope_sql = "".join(f" AND {clause}" for clause in scope_clauses)
    row = query_one(
        f"""
        SELECT COUNT(*) AS c
        FROM help_requests
        WHERE group_id=?
          AND status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'COMPLETED_WITH_FALLBACK')
          AND created_at>=?
          {scope_sql}
        """,
        (group_id, since, *scope_params),
    )
    return int(row["c"] or 0) if row else 0



def _student_help_rate_limit(
    group_id,
    *,
    session_id=None,
    discussion_id=None,
):
    latest = _latest_student_help_activity(
        group_id,
        session_id=session_id,
        discussion_id=discussion_id,
    )
    if latest:
        seconds_since = _seconds_since(latest.get("created_at"))
        if seconds_since is not None and seconds_since < STUDENT_HELP_COOLDOWN_SECONDS:
            wait_seconds = max(1, int(STUDENT_HELP_COOLDOWN_SECONDS - seconds_since))
            return {
                "allowed": False,
                "retry_after_seconds": wait_seconds,
                "reason": f"为避免过度依赖，请先继续讨论 {wait_seconds} 秒后再请求帮助。",
            }

    recent_count = max(
        _count_recent_student_help_logs(
            group_id,
            session_id=session_id,
            discussion_id=discussion_id,
        ),
        _count_recent_student_help_requests(
            group_id,
            session_id=session_id,
            discussion_id=discussion_id,
        ),
    )
    if recent_count >= max(1, int(STUDENT_HELP_MAX_REQUESTS_PER_WINDOW)):
        return {
            "allowed": False,
            "retry_after_seconds": max(1, int(STUDENT_HELP_COOLDOWN_SECONDS)),
            "reason": (
                f"本组在最近 {int(STUDENT_HELP_WINDOW_MINUTES)} 分钟内已多次请求学习助手，"
                "请先根据已有提示继续讨论。"
            ),
        }

    return {
        "allowed": True,
        "retry_after_seconds": 0,
        "reason": None,
    }



def _detect_help_intent(request_text):
    text = _normalized_request_text(request_text)
    if any(token in text for token in ["解释", "什么意思", "看不懂", "要求", "题目"]):
        return "task_explanation"
    if any(token in text for token in ["框架", "思路", "怎么开始", "怎么做", "梳理", "步骤"]):
        return "discussion_framework"
    if any(token in text for token in ["比较", "对比", "观点", "方案", "取舍"]):
        return "compare_views"
    if any(token in text for token in ["总结", "归纳", "汇总", "整理"]):
        return "summarize_progress"
    if any(token in text for token in ["知识", "概念", "背景", "例子", "信息", "线索"]):
        return "limited_knowledge"
    return "next_step"



def _task_summary(task):
    if not task:
        return {
            "question": "请先确认当前小组要完成什么成果。",
            "goal": "把任务目标、关键依据和成员分工说清楚。",
            "output_requirement": "形成可提交的小组成果。",
            "keywords": [],
        }
    return {
        "question": (task.get("question") or task.get("title") or "请先确认任务核心问题。").strip(),
        "goal": (task.get("task_goal") or "把任务目标、关键依据和成员分工说清楚。").strip(),
        "output_requirement": (task.get("output_requirement") or "形成可提交的小组成果。").strip(),
        "keywords": [str(item).strip() for item in (task.get("key_concepts") or []) if str(item).strip()][:3],
    }



def _build_help_message(intent, task, context_summary, condition=None):
    """生成短版回应消息，根据实验组/对照组输出不同内容。
    
    实验组：可以给一个过程性小步骤
    对照组：只给一般性情绪支持
    """
    is_experiment = condition != "control"
    task_info = _task_summary(task)
    question = task_info["question"]
    output_requirement = task_info["output_requirement"]
    
    # Short fallback messages per intent
    experiment_templates = {
        "task_explanation": "可以先用自己的话说一下对任务要求的理解，再对齐共同版本。",
        "discussion_framework": "可以先确定一个小目标，再分配每个人的讨论任务。",
        "compare_views": "不同观点可以各列一条依据，再看哪个更贴近任务要求。",
        "summarize_progress": "可以先请一位同学总结已完成内容，另一位补充遗漏。",
        "limited_knowledge": "可以先抓住任务中的关键词，再把概念和方案对应起来。",
        "next_step": "可以先明确一个5分钟内能完成的小目标，再指定一位记录者。",
    }
    
    control_templates = {
        "task_explanation": "先别着急，任务要求已经给出了，你们可以慢慢研究一下。",
        "discussion_framework": "你们已经有了基本思路，可以按自己的节奏继续。",
        "compare_views": "不同观点都有价值，你们可以慢慢比较。",
        "summarize_progress": "你们已经做了不少工作，可以稍作休整再继续。",
        "limited_knowledge": "你们已经有了一些知识，可以继续借助已有信息推进。",
        "next_step": "你们可以按现在的节奏继续推进，不用着急。",
    }
    
    template = experiment_templates.get(intent) if is_experiment else control_templates.get(intent)
    if template:
        return template
    
    # Default fallback
    if is_experiment:
        return "可以先确定当前要完成的一件事，再分配给具体成员。"
    else:
        return "你们可以慢慢研究，保持目前的进度就很好。"
    

def _state_snapshot(group_id, *, session_id=None):
    state = latest_group_state(group_id, session_id=session_id) or {}
    return {
        "state_code": state.get("state_code") or "unknown",
        "state_label": state.get("state_label") or "未知",
        "confidence": round(float(state.get("state_score") or 0.0), 2),
    }



def _insert_student_help_suggestion(group_id, state, strategy_meta, request_text, context_rows, message, analysis_mode=None, help_request_id=None):
    context_summary = summarize_context(context_rows)
    if help_request_id is not None:
        existing = query_one("SELECT id FROM agent_suggestions WHERE help_request_id=?", (help_request_id,))
        if existing:
            row = query_one("SELECT * FROM agent_suggestions WHERE id=?", (existing["id"],))
            return dict(row) if row else None
    evidence = f"学生主动调用学习助手：{_normalized_request_text(request_text)}"
    suggestion_id = execute(
        """
        INSERT INTO agent_suggestions(
            group_id, state_code, state_label, ssrl_phase, strategy_type,
            confidence, evidence, context_summary, intervention_id, title, message,
            status, source, created_at, sub_category, strategy_id, strategy_name,
            cognitive_load, should_intervene, is_oi_suppressed, condition, intended_strategy_id,
            analysis_mode, llm_analysis_json, trigger_source, decision_id, template_id, help_request_id,
            allowed_auto_push, strategy_version, model_name, prompt_version
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            group_id,
            state["state_code"],
            state["state_label"],
            "student_help_request",
            "student_help",
            state["confidence"],
            evidence,
            context_summary,
            None,
            "学生主动调用学习助手",
            message,
            "pending",
            STUDENT_HELP_SOURCE,
            now_str(),
            strategy_meta["strategy_name"],
            strategy_meta["strategy_id"],
            strategy_meta["strategy_name"],
            "medium",
            1,
            0,
            get_group_condition(group_id),
            strategy_meta["strategy_id"],
            analysis_mode if analysis_mode is not None else STUDENT_HELP_ANALYSIS_MODE,
            None,
            STUDENT_HELP_TRIGGER_SOURCE,
            None,
            strategy_meta["template_id"],
            help_request_id,
            1,
            STUDENT_HELP_STRATEGY_VERSION,
            None,
            None,
        ),
    )
    row = query_one("SELECT * FROM agent_suggestions WHERE id=?", (suggestion_id,))
    return dict(row) if row else None



def request_student_help(group_id, user_id, request_text, client_message_id=None, request_message=None, record_request_message=True):
    normalized_request = _normalized_request_text(request_text)
    message_text = _student_help_message_text(normalized_request)
    print(f"[SERA DEBUG][request_student_help] ENTER: group={group_id}, user={user_id}, request={repr(normalized_request[:80])}")

    if request_message is None and record_request_message:
        request_message = create_message(
            group_id,
            user_id,
            message_text,
            role="student",
            client_message_id=client_message_id,
        )

    from db import get_current_running_session_context
    from agent.help_tasks import _execute_help_flow

    session_ctx = get_current_running_session_context() or {}
    source_message_id = request_message.get("id") if isinstance(request_message, dict) else None
    help_request_message_sequence = (
        request_message.get("sequence") if isinstance(request_message, dict) else None
    )
    scope_conn = db()
    try:
        from services.discussion_scope import resolve_discussion_scope

        scope = resolve_discussion_scope(
            scope_conn,
            group_id=group_id,
            message_id=source_message_id,
            session_id=session_ctx.get("session_id"),
            session_no=session_ctx.get("session_no"),
            task_id=session_ctx.get("task_id"),
            discussion_id=session_ctx.get("discussion_id")
            or session_ctx.get("group_discussion_id"),
            allow_legacy_fallback=False,
        )
    finally:
        scope_conn.close()
    limit_snapshot = _student_help_rate_limit(
        group_id,
        session_id=scope.session_id,
        discussion_id=scope.discussion_id,
    )
    print(f"[SERA DEBUG][request_student_help] rate_limit: allowed={limit_snapshot['allowed']}")
    if not limit_snapshot["allowed"]:
        print(f"[SERA DEBUG][request_student_help] RATE LIMITED: {limit_snapshot['reason']}")
        return {
            "ok": False,
            "rate_limited": True,
            "retry_after_seconds": limit_snapshot["retry_after_seconds"],
            "reason": limit_snapshot["reason"],
            "request_message_id": request_message.get("id") if isinstance(request_message, dict) else None,
            "assistant_log_id": None,
            "assistant_message_id": None,
        }
    help_request_id = execute(
        """
        INSERT INTO help_requests(
            group_id, requester_id, task_id, session_no, session_id, discussion_id,
            status, handling_status, request_text, source_message_id,
            help_request_message_sequence, created_at
        ) VALUES(?,?,?,?,?,?,'QUEUED','queued',?,?,?,?)
        """,
        (
            group_id,
            user_id,
            scope.task_id,
            scope.session_no,
            scope.session_id,
            scope.discussion_id,
            normalized_request,
            source_message_id,
            help_request_message_sequence,
            now_str(),
        ),
    )
    _execute_help_flow(help_request_id)

    help_row = query_one("SELECT * FROM help_requests WHERE id=?", (help_request_id,))
    help_data = dict(help_row) if help_row else {}
    log_row = query_one(
        """
        SELECT id
          FROM intervention_logs
         WHERE help_request_id=?
            OR intervention_id=?
         ORDER BY id DESC
         LIMIT 1
        """,
        (help_request_id, help_data.get("intervention_run_id")),
    )
    status = str(help_data.get("status") or "").upper()
    ok = status in ("COMPLETED", "COMPLETED_WITH_FALLBACK")
    print(f"[SERA DEBUG][request_student_help] DONE: status={status}, message_id={help_data.get('response_message_id')}")
    return {
        "ok": ok,
        "rate_limited": False,
        "retry_after_seconds": 0,
        "reason": None if ok else help_data.get("failure_reason"),
        "intent": help_data.get("intent"),
        "strategy_id": None,
        "help_request_id": help_request_id,
        "request_message_id": request_message.get("id") if isinstance(request_message, dict) else None,
        "assistant_log_id": log_row["id"] if log_row else None,
        "assistant_message_id": help_data.get("response_message_id"),
        "intervention_run_id": help_data.get("intervention_run_id"),
        "suggestion_id": None,
    }
