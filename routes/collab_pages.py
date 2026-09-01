# -*- coding: utf-8 -*-
"""三栏协作学习工作台页面 - 学生端协同学习"""
import json
from html import escape
from flask import redirect, render_template_string, request, url_for
from core import app
from config import *
from db import *
from auth import login_required, current_user, get_tab_token_from_request, create_client_session, get_user_group_id
import os
from agent import get_active_task
from views.base import collab_shell
from services.group_discussion_runtime_service import (
    get_group_discussion_runtime,
    group_discussion_timer_payload,
)


@app.route("/student/collab")
@login_required("student")
def student_collab():
    user = current_user()
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="collab")
        return redirect(url_for("student_collab", tab_token=tab_token,
                                phase=request.args.get("phase", "discussion")))

    phase = (request.args.get("phase") or "discussion").strip().lower()
    if phase not in ("pretest", "discussion", "posttest"):
        phase = "discussion"

    group_id = get_user_group_id(user["id"])
    session_ctx = get_current_running_session_context()

    # Auto-redirect to pretest only for questionnaires published to the
    # current running lesson.
    if phase != "pretest" and session_ctx and session_ctx.get("task_id"):
        has_pending = has_student_pending_questionnaires(
            "pre",
            user["id"],
            session_id=session_ctx["session_id"],
            group_id=group_id,
        )
        if has_pending:
            return redirect(url_for("student_collab", phase="pretest", tab_token=tab_token))

    def _has_active_lesson(ctx):
        return bool(ctx and ctx.get("session_id") and ctx.get("task_id"))

    def _group_has_submitted_deliverable(ctx):
        if not group_id or not _has_active_lesson(ctx):
            return False
        doc = query_one(
            "SELECT status, submitted_at FROM collaborative_documents "
            "WHERE group_id=? AND task_id=? AND session_no=? "
            "ORDER BY id DESC LIMIT 1",
            (group_id, ctx["task_id"], ctx["session_no"]),
        )
        return bool(doc and doc["status"] == "submitted" and doc["submitted_at"])

    lesson_ready = _has_active_lesson(session_ctx)
    deliverable_submitted = _group_has_submitted_deliverable(session_ctx)

    if not lesson_ready:
        phase = "discussion"
    elif phase == "posttest" and not deliverable_submitted:
        return redirect(url_for("student_collab", phase="discussion", tab_token=tab_token))

    group = query_one("SELECT * FROM groups WHERE id=?", (group_id,))
    task = get_active_task() or {}

    participant_code = escape(user["participant_code"] or "")
    display_name = escape(user.get("display_name", "") or "")
    group_code = escape(group["group_code"] if group else "")
    group_name = escape(group["name"] if group else "")
    session_no = get_current_session_no() or 1

    def check_phase_done(stage_key):
        if not session_ctx:
            return True
        qs = list_published_questionnaires_for_student(
            user_id=user["id"],
            session_id=session_ctx["session_id"],
            response_stage=stage_key,
            group_id=group_id,
        )
        for q in qs:
            submitted = query_one(
                "SELECT id FROM questionnaire_submissions "
                "WHERE questionnaire_id=? AND user_id=? AND session_id=? "
                "AND response_stage=? AND status='submitted' LIMIT 1",
                (q["id"], user["id"], session_ctx["session_id"], stage_key),
            )
            response = query_one(
                "SELECT id FROM questionnaire_responses "
                "WHERE questionnaire_id=? AND user_id=? AND session_id=? "
                "AND response_stage=? LIMIT 1",
                (q["id"], user["id"], session_ctx["session_id"], stage_key),
            )
            if not submitted and not response:
                return False
        return True

    pre_done = check_phase_done("pre") if lesson_ready else False
    post_done = check_phase_done("post") if lesson_ready else False
    if session_ctx and lesson_ready:
        post_done = post_done and bool(query_one(
            "SELECT id FROM emotion_checkins "
            "WHERE user_id=? AND group_id=? AND session_id=? AND task_id=? "
            "AND checkin_type='post' LIMIT 1",
            (user["id"], group_id, session_ctx["session_id"], session_ctx["task_id"]),
        ))

    discussion_runtime = None
    discussion_waiting = False
    if phase == "discussion" and lesson_ready and group_id:
        discussion_runtime = get_group_discussion_runtime(session_ctx["session_id"], group_id)
        discussion_waiting = not discussion_runtime or discussion_runtime.get("status") == "waiting"

    phases = [
        ("pretest", "前测", "完成基础问卷", pre_done),
        ("discussion", "讨论", "等待教师发布任务" if not lesson_ready else "小组协作学习", deliverable_submitted),
        ("posttest", "后测", "完成最终问卷", post_done),
    ]

    phase_nav_parts = []
    for i, (key, label, desc, done) in enumerate(phases):
        active = key == phase
        classes = "phase-step"
        if active:
            classes += " active"
        if done and not active:
            classes += " completed"
        indicator = "✓" if done and not active else str(i + 1)
        target_key = key
        if key == "discussion" and not lesson_ready:
            target_key = "discussion"
        elif key == "posttest" and not deliverable_submitted:
            target_key = "discussion"
        href = url_for("student_collab", phase=target_key, tab_token=tab_token)
        current_attr = ' aria-current="step"' if active else ""
        phase_nav_parts.append(
            f'<a href="{href}" class="{classes}"{current_attr}>'
            + f'<span class="phase-indicator">{indicator}</span>'
            + f'<div class="phase-info">'
            + f'<strong>{label}</strong>'
            + f'<small>{desc}</small>'
            + "</div></a>"
        )
        if i < len(phases) - 1:
            conn_cls = "phase-connector" + (" done" if done else "")
            phase_nav_parts.append(f'<div class="{conn_cls}"></div>')

    phase_nav_html = "".join(phase_nav_parts)

    def render_task_badges(items):
        safe = [escape(str(i)) for i in (items or []) if str(i).strip()]
        if not safe:
            return '<span style="color:var(--ink-muted);font-size:12px">暂无</span>'
        return "".join(f'<span class="task-badge">{i}</span>' for i in safe[:6])

    
    def render_structured_task(task):
        import json
        tp = task.get("task_payload") or {}
        if not tp:
            return ""
        parts = []
        # Title section with badges
        task_title = escape(str(task.get("title", "暂无任务")))
        parts.append(f'<div class="task-card">')
        parts.append(f'<div class="task-card-head">')
        parts.append(f'<h2>{task_title}</h2>')
        parts.append(f'<div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">')
        # Neutral summary badge
        parts.append(f'<span class="task-badge" style="background:var(--accent-dim);color:var(--accent);border-color:var(--accent-border)">小组协作决策任务</span>')
        # Budget badges
        budget = tp.get("budget", {})
        budget_total = escape(str(budget.get("total", "")))
        budget_unit = escape(str(budget.get("unit", "万元")))
        min_sel = escape(str(budget.get("min_selected", "")))
        if budget_total:
            parts.append(f'<span class="task-badge">预算上限: {budget_total} {budget_unit}</span>')
        if min_sel:
            parts.append(f'<span class="task-badge">最低选择: {min_sel} 项</span>')
        parts.append(f'<span class="task-badge group-timer-badge">{group_timer_label}</span>')
        parts.append('</div>')
        parts.append('</div>')
        parts.append('<div class="task-card-body">')

        # Background
        bg = tp.get("background") or []
        if bg:
            parts.append('<div class="structured-field"><span class="field-label">任务背景</span><div class="field-value">')
            for p in bg:
                parts.append(f'<p style="margin:0 0 6px">{escape(str(p))}</p>')
            parts.append('</div></div>')

        # Task brief
        tb = tp.get("task_brief") or ""
        if tb:
            parts.append(f'<div class="structured-field"><span class="field-label">本组任务</span><span class="field-value">{escape(str(tb))}</span></div>')

        # Survey
        survey = tp.get("survey") or {}
        s_items = survey.get("items") or []
        if s_items:
            parts.append('<div class="structured-field"><span class="field-label">学生需求调查</span>')
            parts.append('<table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:6px">')
            parts.append('<thead><tr style="border-bottom:1px solid var(--line)"><th style="padding:4px 8px;text-align:left;font-size:11px;">需求内容</th><th style="padding:4px 8px;text-align:right;font-size:11px;">百分比</th></tr></thead><tbody>')
            for item in s_items:
                label = escape(str(item.get("label", "")))
                pct = escape(str(item.get("percent", "")))
                parts.append(f'<tr><td style="padding:3px 8px">{label}</td><td style="padding:3px 8px;text-align:right">{pct}%</td></tr>')
            parts.append('</tbody></table></div>')
        s_note = survey.get("note") or ""
        if s_note:
            parts.append(f'<div class="structured-field"><span class="field-label">调查说明</span><span class="field-value">{escape(str(s_note))}</span></div>')

        # Options
        options = tp.get("options") or []
        if options:
            parts.append('<div class="structured-field"><span class="field-label">可选项目 / 服务</span>')
            parts.append('<table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:6px">')
            parts.append('<thead><tr style="border-bottom:1px solid var(--line)">')
            for h in ["项目名称","费用","单位","主要作用","需要考虑的问题"]:
                parts.append(f'<th style="padding:4px 8px;text-align:left;font-size:11px">{h}</th>')
            parts.append('</tr></thead><tbody>')
            for opt in options:
                name = escape(str(opt.get("name", "")))
                cost = escape(str(opt.get("cost", "")))
                unit = escape(str(opt.get("unit", "")))
                mf = escape(str(opt.get("main_function", "")))
                concern = escape(str(opt.get("concern", "")))
                parts.append(f'<tr><td style="padding:3px 8px">{name}</td><td style="padding:3px 8px">{cost}</td><td style="padding:3px 8px">{unit}</td><td style="padding:3px 8px">{mf}</td><td style="padding:3px 8px">{concern}</td></tr>')
            parts.append('</tbody></table></div>')

        # Constraints
        constraints = tp.get("constraints") or []
        if constraints:
            parts.append('<div class="structured-field"><span class="field-label">必须满足的条件</span>')
            parts.append('<ul style="margin:4px 0;padding-left:18px">')
            for c in constraints:
                parts.append(f'<li style="font-size:13px;color:var(--ink-2);margin-bottom:3px">{escape(str(c))}</li>')
            parts.append('</ul></div>')

        # Discussion questions
        dqs = tp.get("discussion_questions") or []
        if dqs:
            parts.append('<div class="structured-field"><span class="field-label">小组需要讨论</span>')
            parts.append('<ul style="margin:4px 0;padding-left:18px">')
            for dq in dqs:
                parts.append(f'<li style="font-size:13px;color:var(--ink-2);margin-bottom:4px">{escape(str(dq))}</li>')
            parts.append('</ul></div>')

        # Submission requirements
        srs = tp.get("submission_requirements") or []
        if srs:
            parts.append('<div class="structured-field"><span class="field-label">小组提交内容</span>')
            parts.append('<ul style="margin:4px 0;padding-left:18px">')
            for sr in srs:
                parts.append(f'<li style="font-size:13px;color:var(--ink-2);margin-bottom:3px">{escape(str(sr))}</li>')
            parts.append('</ul></div>')

        # Pre-submit checklist
        psc = tp.get("pre_submit_checklist") or []
        if psc:
            parts.append('<div class="structured-field"><span class="field-label">提交前自查</span>')
            parts.append('<ul style="margin:4px 0;padding-left:18px">')
            for item in psc:
                parts.append(f'<li style="font-size:13px;color:var(--ink-2);margin-bottom:3px">{escape(str(item))}</li>')
            parts.append('</ul></div>')

        parts.append('</div>')
        parts.append('</div>')
        return "".join(parts)

    task_title_v = escape(task.get("title", "暂无任务"))
    task_q = escape(task.get("question") or task.get("title") or "暂无任务说明")
    task_goal = escape(task.get("task_goal") or "请等待教师设置当前任务。")
    task_output = escape(task.get("output_requirement") or "完成小组讨论后提交成果。")
    expected_dims = render_task_badges(task.get("expected_dimensions") or [])
    key_concepts = render_task_badges(task.get("key_concepts") or [])
    group_timer = group_discussion_timer_payload(discussion_runtime)

    def _format_group_timer_label():
        status = group_timer.get("group_discussion_status")
        seconds = group_timer.get("group_remaining_seconds")
        if status == "waiting":
            return "讨论计时：等待全组就绪"
        if status == "submitted":
            return "讨论已提交"
        if status in {"timed_out", "closed"} or group_timer.get("group_timed_out"):
            return "讨论已超时"
        if seconds is not None:
            if seconds <= 0:
                return "讨论已超时"
            minutes = max(1, (int(seconds) + 59) // 60)
            return f"讨论剩余约{minutes}分钟"
        limit = task.get("time_limit_minutes") or (session_ctx or {}).get("time_limit_minutes")
        if limit:
            return f"讨论时长 {limit} 分钟，待全组就绪后开始"
        return "讨论计时：待开始"

    group_timer_label = _format_group_timer_label()

    def _first_text(items, fallback=""):
        for item in items or []:
            text = str(item).strip()
            if text:
                return text
        return fallback

    def render_task_summary():
        tp = task.get("task_payload") or {}
        budget = tp.get("budget") or {}
        budget_total = str(budget.get("total", "")).strip()
        budget_unit = str(budget.get("unit", "万元")).strip() or "万元"
        min_sel = str(budget.get("min_selected", "")).strip()
        task_brief = str(tp.get("task_brief") or task_q).strip()
        submit_text = _first_text(tp.get("submission_requirements") or [], task_output)
        discuss_text = _first_text(tp.get("discussion_questions") or [], task_q)
        badges = [
            '<span class="task-badge">当前任务</span>',
            f'<span class="task-badge group-timer-badge" id="groupTimerBadge">{group_timer_label}</span>',
        ]
        if budget_total and budget_total != "0":
            badges.append(f'<span class="task-badge">预算 {escape(budget_total)} {escape(budget_unit)}</span>')
        if min_sel and min_sel != "0":
            badges.append(f'<span class="task-badge">至少选择 {escape(min_sel)} 项</span>')
        return f"""
            <section class="task-brief-panel">
                <div class="task-brief-head">
                    <div class="task-brief-title">
                        <span class="task-kicker">任务摘要</span>
                        <h2>{task_title_v}</h2>
                    </div>
                    <div class="task-brief-badges">{''.join(badges)}</div>
                </div>
                <div class="task-brief-grid">
                    <div class="task-brief-item primary">
                        <span>现在要做</span>
                        <strong>{escape(task_brief or "围绕任务要求完成小组讨论。")}</strong>
                    </div>
                    <div class="task-brief-item">
                        <span>优先讨论</span>
                        <p>{escape(discuss_text or "先明确小组观点，再写入协作文档。")}</p>
                    </div>
                    <div class="task-brief-item">
                        <span>最后提交</span>
                        <p>{escape(submit_text or "完成小组讨论后提交成果。")}</p>
                    </div>
                </div>
            </section>
        """

    def render_chat_focus():
        tp = task.get("task_payload") or {}
        questions = tp.get("discussion_questions") or []
        constraints = tp.get("constraints") or []
        submit_items = tp.get("submission_requirements") or []
        focus = _first_text(questions, task_q)
        constraint = _first_text(constraints, _first_text(task.get("expected_dimensions") or [], "先达成共识，再写入文档"))
        submit_text = _first_text(submit_items, task_output)
        return f"""
            <div class="discussion-focus">
                <div class="discussion-focus-row">
                    <span class="focus-label">当前焦点</span>
                    <strong>{escape(focus or "围绕任务要求开始讨论")}</strong>
                </div>
                <div class="discussion-focus-meta">
                    <span>{escape(constraint)}</span>
                    <span>{escape(submit_text)}</span>
                </div>
            </div>
        """

    mode = phase
    if not lesson_ready:
        mode = "waiting_task"
        mode_label = "等待"
        center_content = f"""
        <div class="center-content">
            <section class="task-brief-panel">
                <div class="task-brief-head">
                    <div class="task-brief-title">
                        <span class="task-kicker">等待中</span>
                        <h2>等待教师发布任务</h2>
                    </div>
                    <div class="task-brief-badges">
                        <span class="task-badge" id="waitingTaskStatus">等待发布</span>
                    </div>
                </div>
                <div class="task-brief-grid">
                    <div class="task-brief-item primary">
                        <span>当前状态</span>
                        <strong>教师端发布任务后，你会自动进入协作讨论。</strong>
                    </div>
                    <div class="task-brief-item">
                        <span>所在小组</span>
                        <p>{group_name or group_code or "暂未分组"}</p>
                    </div>
                    <div class="task-brief-item">
                        <span>你的编号</span>
                        <p>{participant_code or "未读取到编号"}</p>
                    </div>
                </div>
            </section>
            <div class="collaborative-editor-section editor-primary">
                <div class="editor-section-head">
                    <h3>协作学习尚未开始</h3>
                    <div class="editor-status-badge" id="editorStatusBadge">等待发布</div>
                </div>
                <div class="editor-content-area" id="collaborativeEditorArea">
                    <div class="editor-placeholder">
                        <strong>等待教师发布任务</strong>
                        <span>任务发布后，页面会自动刷新进入讨论区。</span>
                    </div>
                </div>
            </div>
        </div>
        """
    elif phase == "pretest":
        mode = "pre"
        mode_label = "前测"
        center_content = """
        <div class="questionnaire-area" id="questionnaireArea">
            <div class="questionnaire-workspace">
                <div class="qa-header">
                    <div>
                        <span class="qa-stage-label">Questionnaire Workspace</span>
                        <h2>研究前测问卷</h2>
                    </div>
                    <button class="chat-action-btn qa-refresh-btn" onclick="window.Questionnaire.reload()">刷新</button>
                </div>
                <aside class="qa-nav-region" aria-label="问卷导航">
                    <div id="questionnaireSummary" class="latest-submission">正在读取问卷...</div>
                </aside>
                <main class="qa-content-region" aria-label="问卷题目">
                    <div id="questionnaireFormBox" class="qa-items"></div>
                    <div id="questionnaireCompletionBox" style="display:none"></div>
                </main>
            </div>
        </div>
        """
    elif phase == "posttest":
        mode = "post"
        mode_label = "后测"
        center_content = """
        <div class="questionnaire-area" id="questionnaireArea">
            <div class="questionnaire-workspace">
                <div class="qa-header">
                    <div>
                        <span class="qa-stage-label">Questionnaire Workspace</span>
                        <h2>研究后测问卷</h2>
                    </div>
                    <button class="chat-action-btn qa-refresh-btn" onclick="window.Questionnaire.reload()">刷新</button>
                </div>
                <aside class="qa-nav-region" aria-label="问卷导航">
                    <div id="questionnaireSummary" class="latest-submission">正在读取问卷...</div>
                </aside>
                <main class="qa-content-region" aria-label="问卷题目">
                    <div id="questionnaireFormBox" class="qa-items"></div>
                    <div id="questionnaireCompletionBox" style="display:none"></div>
                </main>
            </div>
        </div>
        """
    else:
        mode = "discussion_waiting" if discussion_waiting else "discussion"
        mode_label = "讨论"
        # Build task detail HTML based on task type
        if task.get("task_payload"):
            task_detail_html = render_structured_task(task)
        else:
            task_detail_html = f"""\
                                    <div class="task-card">
                                        <div class="task-card-head">
                                            <h2>
                        {task_title_v}
                        </h2>
                                            <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
                                                <span class="task-badge">
                        {group_timer_label}
                        </span>
                                                <span class="task-badge">
                        {participant_code}
                        </span>
                                                <span class="task-badge">
                        {group_code}
                        </span>
                                            </div>
                                        </div>
                                        <div class="task-card-body">
                                            <div class="task-field">
                                                <span class="field-label">核心问题</span>
                                                <span class="field-value">
                        {task_q}
                        </span>
                                            </div>
                                            <div class="task-field">
                                                <span class="field-label">任务目标</span>
                                                <span class="field-value">
                        {task_goal}
                        </span>
                                            </div>
                                            <div class="task-field">
                                                <span class="field-label">成果要求</span>
                                                <span class="field-value">
                        {task_output}
                        </span>
                                            </div>
                                            <div class="task-field">
                                                <span class="field-label">关键维度</span>
                                                <div class="task-badges">
                        {expected_dims}
                        </div>
                                            </div>
                                            <div class="task-field">
                                                <span class="field-label">关键概念</span>
                                                <div class="task-badges">
                        {key_concepts}
                        </div>
                                            </div>
                                        </div>
                                    </div>
        """
        task_summary_html = render_task_summary()
        chat_focus_html = render_chat_focus()

        if discussion_waiting:
            expected_count = discussion_runtime.get("expected_student_count", 0) if discussion_runtime else 0
            ready_count = discussion_runtime.get("ready_student_count", 0) if discussion_runtime else 0
            center_content = f"""\

                    <div class="center-content">
                        {task_summary_html}
                        <div class="collaborative-editor-section editor-primary discussion-waiting-panel">
                            <div class="editor-section-head">
                                <h3>等待小组成员进入讨论</h3>
                                <div class="editor-status-badge" id="editorStatusBadge">等待中</div>
                            </div>
                            <div class="editor-content-area" id="collaborativeEditorArea">
                                <div class="editor-placeholder discussion-waiting-state">
                                    <span>已有 {ready_count}/{expected_count} 名成员准备就绪。全组进入后，讨论计时将自动开始。</span>
                                </div>
                            </div>
                        </div>
                    </div>

        """
        else:
            center_content = f"""\

                    <div class="center-content">
                        {task_summary_html}
                        <details class="task-detail-panel">
                            <summary>
                                <span>完整任务资料</span>
                                <small>背景、方案、约束与自查项</small>
                            </summary>
                            <div class="task-detail-scroll">
            {task_detail_html}
                            </div>
                        </details>
                        <div class="collaborative-editor-section editor-primary">
                            <div class="editor-section-head">
                                <h3>协作文档</h3>
                                <div class="editor-status-badge" id="editorStatusBadge">准备中...</div>
                            </div>
                            <div class="editor-content-area" id="collaborativeEditorArea">
                                <div class="editor-placeholder">
                                    <span>正在加载协作编辑器...</span>
                                </div>
                            </div>
                        </div>
                    </div>

        """
    # Read questionnaire CSS & JS (needed for pretest/posttest phases)
    _qcss_path = os.path.join(os.path.dirname(__file__), os.path.pardir, "static", "student", "questionnaire.css")
    try:
        with open(_qcss_path, "r", encoding="utf-8") as _f:
            _questionnaire_css_content = _f.read()
    except Exception:
        _questionnaire_css_content = ""
    _questionnaire_css = '<style>' + _questionnaire_css_content + '</style>'
    _questionnaire_js = '<script src="/static/student/questionnaire.js"></script>'


    if phase == "discussion" and lesson_ready and not discussion_waiting:
        _right = f"""\
        <div class="collab-right discussion-panel" id="rightPanel">
            <section class="ai-assistant-card" aria-label="学习助手">
            <div class="chat-header">
                <h3><span class="assistant-badge" aria-hidden="true">AI</span> 小组讨论</h3>
                <div class="chat-actions">
                    <button type="button" class="chat-action-btn help-btn" onclick="requestHelp()">向学习助手求助</button>
                </div>
            </div>
            <div class="help-status" id="helpStatus" aria-live="polite"></div>
            </section>
            {chat_focus_html}

            <div class="messages-area conversation-card" id="chatBox">
                <div class="empty-msg-hint">
                    <strong>围绕当前焦点开始</strong>
                    <span>先说出一个方案、理由或疑问；需要时可向学习助手求助</span>
                </div>
            </div>

            <div class="chat-input-area">
                <div class="ai-lock-hint" id="aiLockHint" hidden>学习助手正在介入，请先阅读提示，稍后继续讨论</div>
                <div class="chat-input-row">
                    <textarea id="messageInput" rows="1" placeholder="输入观点、问题或回应；@学习助手 可请求帮助"
                        onkeydown="if(event.key==='Enter'&&!event.shiftKey){{event.preventDefault();sendMessage()}}"></textarea>
                    <button type="button" class="send-btn" aria-label="发送消息" onclick="sendMessage()">→</button>
                </div>
                <div class="chat-input-hint">按 Enter 发送，Shift+Enter 换行</div>
            </div>

            <div class="usage-tips">
                <details open>
                    <summary>使用提示</summary>
                    <div class="tips-content">
                        <p>• 在输入框发表观点，按 Enter 发送</p>
                        <p>• 输入 "@学习助手" 可主动请求 AI 帮助</p>
                        <p>• 点击顶部 "向学习助手求助" 会把当前输入发送给助手分析</p>
                        <p>• AI 助手的回复会直接显示在对话中</p>
                    </div>
                </details>
            </div>
        </div>
        
        """
    else:
        _right = ""
    lesson_label = f"第 {session_no} 课次" if lesson_ready else "等待课次"
    workspace_status = {
        "waiting_task": "等待教师发布任务",
        "discussion_waiting": "等待小组成员就绪",
        "discussion": group_timer_label,
        "pre": "前测问卷",
        "post": "后测问卷",
    }.get(mode, mode_label)
    # The live discussion timer is already shown and updated in the task details.
    # Avoid duplicating a stale timer value in the top-right workspace metadata.
    workspace_status_chip = (
        ""
        if mode == "discussion"
        else f'<span class="student-meta-chip">{workspace_status}</span>'
    )
    body = f"""\

        <header class="student-workspace-header">
            <div class="student-header-main">
                <span class="student-header-kicker">SSRL-ESP 协同学习空间</span>
                <h1>{mode_label}</h1>
            </div>
            <div class="student-header-meta" aria-label="当前学习状态">
                <span class="student-meta-chip">{lesson_label}</span>
                {workspace_status_chip}
                <span class="student-meta-chip">{participant_code or "未读取到编号"}</span>
                <span class="student-meta-chip">{group_name or group_code or "暂未分组"}</span>
            </div>
        </header>
        <div class="collab-left">
            <div class="left-header">
                <h1>协同学习</h1>
                <div class="left-sub">
    {group_name}
    </div>
            </div>
            <div class="left-group-info">
                <span class="info-chip">
    {participant_code}
    </span>
                <span class="info-chip">
    {group_code}
    </span>
            </div>
            <div class="phase-nav">
                
    {phase_nav_html}

            </div>
            <div class="left-footer">
                <a class="logout-link" href="/logout?tab_token=
    {tab_token}
    ">退出登录</a>
            </div>
        </div>

        <div class="collab-center">
            
    {center_content}

        </div>

        {_right}

    """

    has_questionnaire = phase in ("pretest", "posttest")


    script = f"""\
    <script>
    const CURRENT_USER_ID = {user["id"]};
    const GROUP_ID = {group_id};
    const PARTICIPANT_CODE = {json.dumps(participant_code)};
    const DISPLAY_NAME = {json.dumps(display_name)};
    const GROUP_CODE = {json.dumps(group_code)};
    ;
        const TAB_TOKEN = new URLSearchParams(window.location.search).get('tab_token') || '';
        const MODE = {json.dumps(mode)};
    
        function withTabToken(opts) {{
            opts = opts || {{}};
            const h = Object.assign({{}}, opts.headers || {{}});
            if (TAB_TOKEN) h['X-Tab-Token'] = TAB_TOKEN;
            return Object.assign({{}}, opts, {{headers: h}});
        }}
    
        async function fetchJSON(url, opts) {{
            const res = await fetch(url, withTabToken(opts || {{}}));
            const text = await res.text();
            let data = {{}};
            try {{
                data = text ? JSON.parse(text) : {{}};
            }} catch (_err) {{
                data = {{raw: text}};
            }}
            if (!res.ok) {{
                const err = new Error((data && (data.error || data.reason)) || '请求失败');
                err.status = res.status;
                err.code = data && data.code;
                err.data = data;
                throw err;
            }}
            return data;
        }}
    
        function escapeHtml(s) {{
            return (s || '').replace(/[&<>"']/g, function(m) {{
                return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}}[m];
            }});
        }}
    
        function makeClientMessageId() {{
            if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
            return 'msg-' + Date.now() + '-' + Math.random().toString(16).slice(2);
        }}

        function setHelpStatus(message, tone) {{
            const el = document.getElementById('helpStatus');
            if (!el) return;
            el.textContent = message || '';
            el.className = 'help-status' + (tone ? ' ' + tone : '');
        }}

        function isAiLockError(error) {{
            return !!error && (error.status === 423 || error.code === 'ROOM_AI_INTERVENING' ||
                (error.data && error.data.code === 'ROOM_AI_INTERVENING'));
        }}

        function normalizeAiLock(data) {{
            const lock = (data && data.ai_lock) || (data && data.data && data.data.ai_lock) || null;
            const room = (data && data.room) || null;
            if (lock) return lock;
            return {{
                locked: !!(room && room.state === 'AI_INTERVENING'),
                reason: room && room.state === 'AI_INTERVENING' ? 'ROOM_AI_INTERVENING' : null,
                active_intervention_run_id: room ? room.active_intervention_run_id : null,
                lock_expires_at: room ? room.lock_expires_at : null
            }};
        }}

        function formatLockMessage(lock) {{
            if (!lock || !lock.locked) return '';
            let msg = '学习助手正在介入，请先阅读提示，稍后继续讨论';
            if (lock.lock_expires_at) {{
                msg += '（预计很快恢复）';
            }}
            return msg;
        }}

        function setComposerLocked(lock) {{
            const normalized = normalizeAiLock({{ai_lock: lock || {{locked: false}}}});
            const locked = !!normalized.locked;
            isAILocked = locked;
            const input = document.getElementById('messageInput');
            const sendBtn = document.querySelector('.send-btn');
            const helpBtn = document.querySelector('.help-btn');
            const hint = document.getElementById('aiLockHint');
            if (input) {{
                input.readOnly = locked;
                input.setAttribute('aria-disabled', locked ? 'true' : 'false');
                input.classList.toggle('ai-locked', locked);
                input.placeholder = locked
                    ? '学习助手正在介入，请稍后继续讨论'
                    : '输入观点、问题或回应；@学习助手 可请求帮助';
            }}
            if (sendBtn) sendBtn.disabled = locked || sendingMessage;
            if (helpBtn) {{
                if (!helpBtn.dataset.originalText) helpBtn.dataset.originalText = helpBtn.textContent;
                helpBtn.disabled = locked;
                if (!locked) helpBtn.textContent = helpBtn.dataset.originalText || helpBtn.textContent;
            }}
            if (hint) {{
                hint.hidden = !locked;
                hint.textContent = formatLockMessage(normalized);
            }}
            if (locked) {{
                setHelpStatus('学习助手正在介入，输入框已暂时锁定。', 'pending');
            }} else {{
                const statusEl = document.getElementById('helpStatus');
                if (statusEl && statusEl.classList.contains('pending') && /学习助手正在介入|输入框已暂时锁定/.test(statusEl.textContent || '')) {{
                    setHelpStatus('', '');
                }}
            }}
        }}

        function applyAiLockFromError(error) {{
            const lock = (error && error.data && error.data.ai_lock) || {{locked: true, reason: 'ROOM_AI_INTERVENING'}};
            setComposerLocked(lock);
        }}
    
        let lastMessageId = 0;
        let currentSequence = 0;
        let roomState = null;
        let roomVersion = 0;
        let isAILocked = false;
        const STUDENT_SYNC_MS = 3000;
        const STUDENT_SYNC_JITTER_MS = 800;
        let sendingMessage = false;
        let syncInFlight = false;
        let syncStarted = false;
        let syncTimer = null;
        let syncFailCount = 0;
    
        function buildStudentSyncUrl() {{
            const params = new URLSearchParams();
            params.set("after_message_id", String(lastMessageId || 0));
            params.set("after_sequence", String(currentSequence || 0));
            return "/api/student/sync?" + params.toString();
        }}

        function applyRoomEvents(data) {{
            const events = (data && data.events) || {{}};
            const room = (data && data.room) || events.room;
            if (room) {{
                roomState = room.state;
                roomVersion = room.version || roomVersion;
                isAILocked = room.state === "AI_INTERVENING";
            }}
            const nextSequence = data && (data.event_sequence !== undefined ? data.event_sequence : events.next_sequence);
            if (nextSequence !== null && nextSequence !== undefined) {{
                currentSequence = Math.max(currentSequence, Number(nextSequence) || 0);
            }}
        }}
    
        function applyMessages(data) {{
                const chat = (data && data.chat) || {{}};
                const messages = chat.messages || (data && data.messages) || [];
                const latestId = data && (data.latest_message_id !== undefined ? data.latest_message_id : chat.latest_id);
                const wasInitial = lastMessageId === 0;
                const box = document.getElementById('chatBox');
                if (latestId !== null && latestId !== undefined) {{
                    lastMessageId = Math.max(lastMessageId, Number(latestId) || 0);
                }}
                if (!box) return;
                const distanceBefore = box.scrollHeight - box.scrollTop - box.clientHeight;
                const shouldStick = distanceBefore < 120;
                const html = messages.map(m => {{
                    const isSelf = m.user_id === CURRENT_USER_ID && m.role !== 'agent';
                    const isAgent = m.role === 'agent';
                    const isSystem = m.role === 'system' || m.role === 'teacher';
                    const cls = isSelf ? 'self' : (isAgent ? 'agent' : (isSystem ? 'system' : ''));
                    const roleLabel = isAgent ? 'SERA助手' : escapeHtml(m.display_name || m.participant_code || '');
                    const avatarInitial = roleLabel ? roleLabel.charAt(0).toUpperCase() : '?';
                    const timeStr = m.created_at ? m.created_at.slice(11, 19) : '';
                    return '<div class="msg-item ' + cls + '" data-mid="' + m.id + '">' +
                        '<div class="msg-avatar">' + avatarInitial + '</div>' +
                        '<div class="msg-bubble">' +
                        '<div class="msg-meta"><span class="msg-name">' + roleLabel + '</span><span class="msg-time">' + timeStr + '</span></div>' +
                        '<div class="msg-text">' + escapeHtml(m.content) + '</div>' +
                        '</div></div>';
                }}).join('');
    
                if (wasInitial && html) {{
                    box.innerHTML = html || '<div class="empty-msg-hint"><strong>围绕当前焦点开始</strong><span>先说出一个方案、理由或疑问；需要时可向学习助手求助</span></div>';
                }} else if (!wasInitial && html) {{
                    box.insertAdjacentHTML('beforeend', html);
                    while (box.children.length > 350) box.removeChild(box.firstElementChild);
                }}
                if (shouldStick) box.scrollTop = box.scrollHeight;
        }}

        function applyStudentSyncData(data) {{
            if (!data) return;
            applyRoomEvents(data);
            setComposerLocked(normalizeAiLock(data));
            applyMessages(data);
            updateGroupTimerBadge(data);
            handleWaitingTaskSync(data);
            handleDiscussionGateSync(data);
            handleDocumentStatusSync(data);
        }}

        async function runStudentSync() {{
            if (syncInFlight) return null;
            syncInFlight = true;
            try {{
                const data = await fetchJSONWithTimeout(buildStudentSyncUrl(), {{}}, 10000);
                syncFailCount = 0;
                applyStudentSyncData(data);
                return data;
            }} catch (e) {{
                syncFailCount++;
                return null;
            }} finally {{
                syncInFlight = false;
            }}
        }}

        function nextStudentSyncDelay() {{
            const baseDelay = syncFailCount > 2
                ? Math.min(2000 * (1 << Math.min(syncFailCount - 3, 4)), 30000)
                : STUDENT_SYNC_MS;
            return baseDelay + Math.floor(Math.random() * STUDENT_SYNC_JITTER_MS);
        }}

        function scheduleNextStudentSync(delayMs) {{
            if (!syncStarted) return;
            if (syncTimer) clearTimeout(syncTimer);
            const delay = delayMs === null || delayMs === undefined ? nextStudentSyncDelay() : delayMs;
            syncTimer = setTimeout(async function() {{
                await runStudentSync();
                scheduleNextStudentSync();
            }}, delay);
        }}

        function startStudentSyncLoop() {{
            if (syncStarted) return;
            syncStarted = true;
            scheduleNextStudentSync(Math.floor(Math.random() * STUDENT_SYNC_JITTER_MS));
        }}
    
        async function sendMessage() {{
            const input = document.getElementById('messageInput');
            if (!input) return;
            if (isAILocked) {{
                setHelpStatus('学习助手正在介入，请稍后再发送。', 'pending');
                await runStudentSync();
                return;
            }}
            const content = input.value.trim();
            if (!content) return;
            if (sendingMessage) return;
            sendingMessage = true;
            setComposerLocked({{locked: isAILocked, reason: isAILocked ? 'ROOM_AI_INTERVENING' : null}});
            try {{
                const data = await fetchJSON('/api/message', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{group_id: GROUP_ID, content, client_message_id: makeClientMessageId()}})
                }});
                input.value = '';
                await runStudentSync();
                scheduleNextStudentSync(500);
            }} catch (e) {{
                if (isAiLockError(e)) {{
                    applyAiLockFromError(e);
                    scheduleNextStudentSync(500);
                }} else {{
                    alert('发送失败，请重试');
                }}
            }} finally {{
                sendingMessage = false;
                setComposerLocked({{locked: isAILocked, reason: isAILocked ? 'ROOM_AI_INTERVENING' : null}});
            }}
        }}
    
        async function requestHelp() {{
            const btn = document.querySelector('.help-btn');
            if (btn && btn.disabled) return; // prevent double-click
            const input = document.getElementById('messageInput');
            if (isAILocked) {{
                setHelpStatus('学习助手正在介入，请稍后再请求帮助。', 'pending');
                await runStudentSync();
                return;
            }}
            const requestText = input.value.trim() || '请帮助我们梳理下一步。';
            if (btn) {{
                btn.disabled = true;
                btn.dataset.originalText = btn.dataset.originalText || btn.textContent;
                btn.textContent = '已提交';
            }}
            setHelpStatus('已提交给学习助手，正在分析当前讨论。', 'pending');
            try {{
                const res = await fetch('/api/student/help', withTabToken({{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{group_id: GROUP_ID, request_text: requestText, client_message_id: makeClientMessageId()}})
                }}));
                const d = await res.json().catch(() => ({{}}));
                if (res.status === 423 || (d && d.code === 'ROOM_AI_INTERVENING')) {{
                    applyAiLockFromError({{status: res.status, code: d.code, data: d}});
                    scheduleNextStudentSync(500);
                    return;
                }}
                if (res.status === 429 || (d && d.rate_limited)) {{
                    const msg = (d && (d.reason || d.error)) || '请稍后再试';
                    setHelpStatus(msg, 'error');
                    alert(msg);
                    if (btn) {{
                        btn.disabled = false;
                        btn.textContent = btn.dataset.originalText || '向学习助手求助';
                    }}
                    return;
                }}
                if (!res.ok) {{
                    throw new Error((d && (d.error || d.reason)) || '请求失败');
                }}
                input.value = '';
                setComposerLocked({{locked: true, reason: 'ROOM_AI_INTERVENING'}});
                setHelpStatus('学习助手正在整理回复，稍后会出现在讨论区。', 'pending');
                // Show queued status in chat
                const box = document.getElementById('chatBox');
                if (box) {{
                    const hint = document.createElement('div');
                    hint.className = 'msg-item agent';
                    hint.innerHTML = '<div class="msg-avatar">S</div><div class="msg-bubble"><div class="msg-meta"><span class="msg-name">SERA助手</span></div><div class="msg-text" style="color:var(--ink-muted);font-style:italic">已提交，学习助手正在分析……</div></div>';
                    box.appendChild(hint);
                    box.scrollTop = box.scrollHeight;
                }}
                await runStudentSync();
                scheduleNextStudentSync(1000);
                // Re-enable button after a short cooldown
                if (btn) setTimeout(() => {{
                    if (!isAILocked) {{
                        btn.disabled = false;
                        btn.textContent = btn.dataset.originalText || '向学习助手求助';
                        setHelpStatus('', '');
                    }}
                }}, 5000);
            }} catch (e) {{
                setHelpStatus('请求失败，请重试。', 'error');
                alert('请求失败，请重试');
                if (btn) {{
                    btn.disabled = isAILocked;
                    btn.textContent = btn.dataset.originalText || '向学习助手求助';
                }}
            }}
        }}

        // --- 协作编辑器初始化 ---
        const WS_URL = "{COLLAB_WS_EXTERNAL_URL}";
        const USER_COLORS = ["#e74c3c","#3498db","#2ecc71","#f39c12","#9b59b6","#1abc9c","#e67e22","#2980b9","#d35400","#27ae60","#8e44ad","#16a085"];
        let activeDocumentId = null;
        let discussionWaitingForPeers = MODE === "discussion_waiting";
        let posttestRedirectChecking = false;
        let posttestRedirectDone = false;
        let groupTimeoutResolving = false;
        let discussionEnterPromise = null;
        let discussionEnterData = null;

        // fetchJSON with timeout for discussion/status requests.
        async function fetchJSONWithTimeout(url, opts, timeoutMs) {{
            if (timeoutMs == null) timeoutMs = 10000;
            var ctrl = new AbortController();
            var tid = setTimeout(function() {{ ctrl.abort(); }}, timeoutMs);
            try {{
                var res = await fetch(url, withTabToken(Object.assign({{}}, opts || {{}}, {{ signal: ctrl.signal }})));
                if (!res.ok) throw new Error("\u8bf7\u6c42\u5931\u8d25");
                return await res.json();
            }} catch (e) {{
                if (e.name === "AbortError") throw new Error("\u8bf7\u6c42\u8d85\u65f6");
                throw e;
            }} finally {{
                clearTimeout(tid);
            }}
        }}

        function formatGroupTimerLabel(data) {{
            const status = data && data.group_discussion_status;
            const seconds = data ? data.group_remaining_seconds : null;
            if (status === "waiting") return "\u8ba8\u8bba\u8ba1\u65f6\uff1a\u7b49\u5f85\u5168\u7ec4\u5c31\u7eea";
            if (status === "submitted") return "\u8ba8\u8bba\u5df2\u63d0\u4ea4";
            if (status === "timed_out" || status === "closed" || (data && data.group_timed_out)) return "\u8ba8\u8bba\u5df2\u8d85\u65f6";
            if (seconds !== null && seconds !== undefined) {{
                if (seconds <= 0) return "\u8ba8\u8bba\u5df2\u8d85\u65f6";
                const minutes = Math.max(1, Math.ceil(Number(seconds) / 60));
                return "\u8ba8\u8bba\u5269\u4f59\u7ea6" + minutes + "\u5206\u949f";
            }}
            return "\u8ba8\u8bba\u8ba1\u65f6\uff1a\u5f85\u5f00\u59cb";
        }}

        function updateGroupTimerBadge(data) {{
            const badge = document.getElementById("groupTimerBadge") || document.querySelector(".group-timer-badge");
            if (badge) badge.textContent = formatGroupTimerLabel(data || {{}});
        }}

        async function enterDiscussionStage() {{
            if (discussionEnterPromise) return discussionEnterPromise;
            discussionEnterPromise = fetchJSONWithTimeout('/api/discussion/enter', {{method: 'POST'}}, 10000)
                .then(function(data) {{
                    discussionEnterData = data;
                    updateGroupTimerBadge(data);
                    return data;
                }})
                .catch(function(e) {{
                    discussionEnterPromise = null;
                    throw e;
                }});
            return discussionEnterPromise;
        }}

        async function resolveGroupTimeout(documentId) {{
            if (groupTimeoutResolving) return;
            groupTimeoutResolving = true;
            let finalContent = {{}};
            try {{
                const provider = window.__COLLAB_EDITOR_GET_FINAL_CONTENT__;
                const snapshot = typeof provider === "function" ? provider(documentId) : null;
                if (snapshot && typeof snapshot === "object") finalContent = snapshot;
            }} catch (e) {{}}
            const statusBadge = document.getElementById("editorStatusBadge");
            const editorArea = document.getElementById("collaborativeEditorArea");
            if (statusBadge) statusBadge.textContent = "\u8ba8\u8bba\u5df2\u8d85\u65f6";
            if (editorArea) {{
                editorArea.innerHTML = '<div class="editor-submitted-msg"><div class="submitted-icon">\\u2713</div><div>\\u8ba8\\u8bba\\u5df2\\u8d85\\u65f6\\uff0c\\u6b63\\u5728\\u9501\\u5b9a\\u5e76\\u8fdb\\u5165\\u540e\\u6d4b</div></div>';
            }}
            try {{
                await fetchJSON('/api/collaborative-documents/' + documentId + '/submit/auto-timeout', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify(finalContent)
                }});
            }} catch (e) {{
                // Huey's server-side scan remains the retry path for offline or
                // transient client failures; keep the post-test transition.
            }}
            setTimeout(function() {{
                window.location.href = '/student/collab?phase=posttest&tab_token=' + encodeURIComponent(TAB_TOKEN);
            }}, 1200);
        }}

        function handleWaitingTaskSync(data) {{
            if (MODE !== "waiting_task") return;
            const statusBadge = document.getElementById("waitingTaskStatus");
            const editorStatus = document.getElementById("editorStatusBadge");
            if (data && data.status === "ok" && data.session_open && data.session && data.session.task_id) {{
                if (statusBadge) statusBadge.textContent = "\u4efb\u52a1\u5df2\u53d1\u5e03";
                if (editorStatus) editorStatus.textContent = "\u6b63\u5728\u8fdb\u5165...";
                window.location.href = '/student/collab?phase=discussion&tab_token=' + encodeURIComponent(TAB_TOKEN);
                return;
            }}
            if (statusBadge) statusBadge.textContent = "\u7b49\u5f85\u53d1\u5e03";
        }}

        function handleDiscussionGateSync(data) {{
            if (!discussionWaitingForPeers) return;
            const statusBadge = document.getElementById("editorStatusBadge");
            const discussionStatus = data && (data.group_discussion_status || (data.discussion && data.discussion.status));
            const runtime = data && data.group_discussion;
            if (discussionStatus && discussionStatus !== "waiting" && data.document) {{
                discussionWaitingForPeers = false;
                if (statusBadge) statusBadge.textContent = "\u5c0f\u7ec4\u5df2\u5c31\u7eea";
                window.location.href = '/student/collab?phase=discussion&tab_token=' + encodeURIComponent(TAB_TOKEN);
                return;
            }}
            if (runtime && statusBadge) {{
                statusBadge.textContent = "\u7b49\u5f85\u4e2d " + (runtime.ready_student_count || 0) + "/" + (runtime.expected_student_count || 0);
            }}
        }}

        async function checkPosttestRedirect() {{
            if (posttestRedirectChecking || posttestRedirectDone) return;
            posttestRedirectChecking = true;
            try {{
                const resp = await fetchJSON("/api/student/questionnaires?stage=post");
                var pendingPost = resp.post_checkin_completed === false;
                for (var i = 0; i < (resp.questionnaires || []).length; i++) {{
                    var q = resp.questionnaires[i];
                    if ((q.allowed_stages || []).indexOf("post") !== -1 &&
                        (q.completed_stages || []).indexOf("post") === -1) {{
                        pendingPost = true;
                        break;
                    }}
                }}
                if (pendingPost) {{
                    posttestRedirectDone = true;
                    setTimeout(function() {{
                        window.location.href = '/student/collab?phase=posttest&tab_token=' + encodeURIComponent(TAB_TOKEN);
                    }}, 2000);
                }}
            }} catch (e) {{
            }} finally {{
                posttestRedirectChecking = false;
            }}
        }}

        async function handleDocumentStatusSync(data) {{
            const doc = data && data.document;
            const documentId = activeDocumentId || (doc && doc.id);
            if (doc && !activeDocumentId && doc.status !== "submitted" && doc.status !== "locked") {{
                activeDocumentId = doc.id;
            }}
            if (documentId && data && (data.group_timed_out || (data.group_remaining_seconds !== null && data.group_remaining_seconds !== undefined && data.group_remaining_seconds <= 0))) {{
                await resolveGroupTimeout(documentId);
                return;
            }}
            if (doc && (doc.status === "submitted" || doc.status === "locked")) {{
                const badge = document.getElementById("editorStatusBadge");
                if (badge) badge.textContent = "\u5df2\u63d0\u4ea4";
                await checkPosttestRedirect();
            }}
        }}

        async function initCollaborativeEditor() {{
            if (document.getElementById("editorStatusBadge") === null) return;
            const statusBadge = document.getElementById("editorStatusBadge");
            const editorArea = document.getElementById("collaborativeEditorArea");

            try {{
                statusBadge.textContent = "\u6b63\u5728\u83b7\u53d6\u534f\u4f5c\u6587\u6863...";
                const docData = await enterDiscussionStage();
                const doc = docData.document;
                updateGroupTimerBadge(docData);

                if (docData.waiting || !doc) {{
                    statusBadge.textContent = "\u7b49\u5f85\u5c0f\u7ec4\u6210\u5458...";
                    if (editorArea) {{
                        editorArea.innerHTML = '<div class="editor-placeholder discussion-waiting-state"><span>\\u7b49\\u5f85\\u5168\\u7ec4\\u6210\\u5458\\u8fdb\\u5165\\u8ba8\\u8bba</span></div>';
                    }}
                    discussionWaitingForPeers = true;
                    scheduleNextStudentSync(1000);
                    return;
                }}

                if (docData.group_timed_out || (docData.group_remaining_seconds !== null && docData.group_remaining_seconds !== undefined && docData.group_remaining_seconds <= 0)) {{
                    await resolveGroupTimeout(doc.id);
                    return;
                }}

                if (doc.status === "submitted" || doc.status === "locked") {{
                    statusBadge.textContent = "\u5df2\u63d0\u4ea4";
                    editorArea.innerHTML = '<div class="editor-submitted-msg"><div class="submitted-icon">\\u2713</div><div>\\u6587\\u6863\\u5df2\\u63d0\\u4ea4</div></div>';
                    await checkPosttestRedirect();
                    return;
                }}

                statusBadge.textContent = "\u52a0\u8f7d\u4e2d...";
                console.log("[collab-frontend] connect-start");
                if (typeof window.__COLLAB_DIAG__ !== "undefined" && window.__COLLAB_DIAG__) {{ console.log("[collab-diagnosis] connect-start"); }}

                var displayName = PARTICIPANT_CODE || GROUP_CODE || "\u7528\u6237";
                var userColor = USER_COLORS[CURRENT_USER_ID % USER_COLORS.length];

                activeDocumentId = doc.id;
                const config = {{
                    documentId: doc.id,
                    taskId: doc.task_id,
                    sessionNo: doc.session_no,
                    apiBase: "/api/collaborative-documents",
                    wsUrl: WS_URL,
                    displayName: displayName,
                    participantCode: PARTICIPANT_CODE,
                    permission: "edit",
                    userId: CURRENT_USER_ID,
                    userColor: userColor,
                }};

                const configScript = document.createElement("script");
                configScript.id = "collab-editor-config";
                configScript.type = "application/json";
                configScript.textContent = JSON.stringify(config);
                document.body.appendChild(configScript);

                editorArea.innerHTML = "";
                const editorApp = document.createElement("div");
                editorApp.id = "collaborative-editor-app";
                editorArea.appendChild(editorApp);

                if (!document.querySelector('link[href="/static/collaborative-editor/editor.css"]')) {{
                    const link = document.createElement("link");
                    link.rel = "stylesheet";
                    link.href = "/static/collaborative-editor/editor.css";
                    document.head.appendChild(link);
                }}

                statusBadge.textContent = "\u52a0\u8f7d\u4e2d...";
                const script = document.createElement("script");
                script.src = "/static/collaborative-editor/editor.js";
                script.onload = function() {{
                    console.log("[collab-frontend] editor-loaded");
                    if (typeof window.__COLLAB_DIAG__ !== "undefined" && window.__COLLAB_DIAG__) {{ console.log("[collab-diagnosis] editor-loaded"); }}
                    statusBadge.textContent = "\u5c31\u7eea"; }};
                script.onerror = function() {{
                    statusBadge.textContent = "\u52a0\u8f7d\u5931\u8d25";
                    editorArea.innerHTML = '<div class="editor-error-msg">\\u534f\\u4f5c\\u7f16\\u8f91\\u5668\\u52a0\\u8f7d\\u5931\\u8d25\\uff0c\\u8bf7\\u5237\\u65b0\\u9875\\u9762\\u91cd\\u8bd5</div>';
                }};
                document.body.appendChild(script);

            }} catch (e) {{
                console.error("[CollabEditor] Init failed:", e);
                if (statusBadge) statusBadge.textContent = "\u521d\u59cb\u5316\u5931\u8d25";
                if (editorArea) editorArea.innerHTML = '<div class="editor-error-msg">\\u534f\\u4f5c\\u7f16\\u8f91\\u5668\\u521d\\u59cb\\u5316\\u5931\\u8d25: ' + escapeHtml(e.message || '\\u672a\\u77e5\\u9519\\u8bef') + '</div>';
            }}
        }}

        async function ensureDiscussionEntered() {{
            try {{
                const data = await enterDiscussionStage();
                if (data && !data.waiting && data.document) {{
                    discussionWaitingForPeers = false;
                    updateGroupTimerBadge(data);
                    window.location.href = '/student/collab?phase=discussion&tab_token=' + encodeURIComponent(TAB_TOKEN);
                    return;
                }}
                discussionWaitingForPeers = data && (data.waiting || !data.document);
                applyStudentSyncData(data);
                scheduleNextStudentSync(1000);
            }} catch (e) {{}}
        }}

        let qPayload = [];
        let openQId = null;
        let openQStage = null;
    
        function stageLabel(stage) {{ return stage === 'pre' ? '前测' : '后测'; }}
        function findQ(id) {{ return qPayload.find(item => item.id === id) || null; }}
        function allDone() {{
            const applicable = qPayload.filter(q => (q.allowed_stages || []).includes(MODE));
            return applicable.length > 0 && applicable.every(q => (q.completed_stages || []).includes(MODE));
        }}
    
        function renderQ() {{
            const summary = document.getElementById('questionnaireSummary');
            const formBox = document.getElementById('questionnaireFormBox');
            const doneBox = document.getElementById('questionnaireCompletionBox');
            if (!summary) return;
            if (!qPayload.length) {{
                summary.innerHTML = '<span style="color:var(--ink-muted);font-size:12px">当前课时暂无已启用的' + stageLabel(MODE) + '问卷</span>';
                if (formBox) formBox.innerHTML = '';
                if (doneBox) {{
                    doneBox.style.display = 'block';
                    doneBox.innerHTML = '<div style="padding:12px 14px;border-radius:var(--radius);background:var(--paper-strong);border:1px solid var(--line-soft);text-align:center">暂无问卷需要填写。<br><a href="/student/collab?phase=discussion&tab_token=' + encodeURIComponent(TAB_TOKEN) + '" style="color:var(--accent);font-weight:600">返回讨论</a></div>';
                }}
                return;
            }}
            if (allDone()) {{
                summary.innerHTML = '<div style="padding:10px 14px;border-radius:var(--radius);background:var(--success-bg);border:1px solid rgba(74,124,89,.2);color:var(--success-text);font-weight:600">全部' + stageLabel(MODE) + '问卷已完成</div>';
                if (formBox) formBox.innerHTML = '';
                if (doneBox) {{
                    doneBox.style.display = 'block';
                    doneBox.innerHTML = '<div style="text-align:center;margin-top:8px"><a href="/student/collab?phase=discussion&tab_token=' + encodeURIComponent(TAB_TOKEN) + '" class="submission-btn">返回讨论</a></div>';
                }}
                return;
            }}
            if (doneBox) doneBox.style.display = 'none';
            const pendingList = qPayload.filter(q => !(q.completed_stages || []).includes(MODE) && (q.allowed_stages || []).includes(MODE));
            if (summary) {{
                summary.innerHTML = pendingList.map(q => {{
                    const done = (q.completed_stages || []).includes(MODE);
                    return '<button class="qa-item-btn" data-qid="' + q.id + '" data-stage="' + MODE + '" onclick="openQ(+this.dataset.qid,this.dataset.stage)" style="' + (done ? 'opacity:0.6' : '') + '">' +
                        '<span>' + escapeHtml(q.title || '') + '</span>' +
                        '<span class="qa-status' + (done ? ' done' : '') + '">' + (done ? '已完成' : '待填写') + '</span></button>';
                }}).join('');
            }}
            if (!openQId || !openQStage) {{
                if (formBox) formBox.innerHTML = '<span style="color:var(--ink-muted);font-size:12px;padding:8px 0">请选择上方一份问卷进行填写。</span>';
                return;
            }}
            const q = findQ(openQId);
            if (!q) {{ if (formBox) formBox.innerHTML = '<span style="color:var(--ink-muted);font-size:12px">该问卷已不可用</span>'; return; }}
            const items = (q.items || []).map(item => {{
                const key = openQStage + ':' + item.id;
                const existing = (q.existing_responses || {{}})[key];
                const opts = Array.from({{length: q.scale_max}}, (_, i) => {{
                    const v = i + 1;
                    return '<option value="' + v + '"' + (existing == v ? ' selected' : '') + '>' + v + '</option>';
                }}).join('');
                return '<div class="qa-item-row"><span class="qa-label">' + escapeHtml(item.dimension_label || item.prompt_text) + '</span>' +
                    '<select data-item-id="' + item.id + '">' + opts + '</select></div>';
            }}).join('');
            if (formBox) {{
                formBox.innerHTML = '<div class="qa-form-card">' +
                    '<div class="qa-form-head"><h3>' + escapeHtml(q.title || '') + '</h3><span class="task-badge">1-' + q.scale_max + '</span></div>' +
                    '<div class="qa-form-body">' + items +
                    '<div class="qa-actions">' +
                    '<button class="submission-btn" onclick="submitQ()">提交' + stageLabel(openQStage) + '</button>' +
                    '<button class="chat-action-btn" onclick="closeQ()">收起</button></div></div></div>';
            }}
        }}
    
        function openQ(id, stage) {{ openQId = id; openQStage = stage; renderQ(); }}
        function closeQ() {{ openQId = null; openQStage = null; renderQ(); }}
    
        async function loadQ() {{
            try {{
                const data = await fetchJSON('/api/student/questionnaires');
                qPayload = data.questionnaires || [];
                console.log('[SSRL] loadQ: questionnaires loaded', qPayload.length);
                if (openQId && !findQ(openQId)) {{ openQId = null; openQStage = null; }}
                renderQ();
            }} catch (e) {{
                console.error("[SSRL] loadQ error:", e);
            }}
        }}
    
        async function submitQ() {{
            const q = findQ(openQId);
            if (!q || !openQStage) return;
            const selects = document.querySelectorAll('.qa-form-body select[data-item-id]');
            const responses = {{}};
            selects.forEach(sel => {{ const id = sel.dataset.itemId; if (id) responses[id] = parseInt(sel.value, 10); }});
            try {{
                await fetchJSON('/api/student/questionnaires/' + q.id + '/submit', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{response_stage: openQStage, responses}})
                }});
                openQId = null;
                openQStage = null;
                await loadQ();
            }} catch (e) {{
                alert('提交失败，请重试');
            }}
        }}
    
        window.sendMessage = sendMessage;
        window.requestHelp = requestHelp;

        startStudentSyncLoop();
        if (MODE === "discussion") {{
            setTimeout(initCollaborativeEditor, 200);
        }}
        if (MODE === "discussion_waiting") {{
            ensureDiscussionEntered();
        }}
        // Visibility-aware sync: the next scheduled sync is delayed while hidden.
        document.addEventListener('visibilitychange', function() {{
            if (document.hidden) scheduleNextStudentSync(STUDENT_SYNC_MS * 2);
            else scheduleNextStudentSync(0);
        }});
        
    """

    if has_questionnaire:
        # Replace entire script with questionnaire module
        script = _questionnaire_js + f"""<script>
    const CURRENT_USER_ID = {user["id"]};
    const GROUP_ID = {group_id};
    const TAB_TOKEN = new URLSearchParams(window.location.search).get('tab_token') || '';
    const MODE = '{mode}';
    window.Questionnaire.init(MODE, TAB_TOKEN);
    </script>"""

    if not has_questionnaire:
        script += """
        </script>
        """

    # Include collab CSS
    with open(os.path.join(os.path.dirname(__file__), os.path.pardir, "static", "collab.css"), "r", encoding="utf-8") as _css_f:
        _collab_css_content = _css_f.read()


    q_extra = _questionnaire_css if phase in ("pretest", "posttest") else ""
    shell_classes = [
        f"phase-{phase}",
        f"mode-{mode.replace('_', '-')}",
        "has-chat-panel" if _right else "no-chat-panel",
    ]
    return render_template_string(collab_shell(
        "协同学习 - SSRL-ESP",
        body + q_extra + '<style>' + _collab_css_content + '</style>',
        script,
        shell_class=" ".join(shell_classes),
    ))
