# -*- coding: utf-8 -*-
"""页面路由：首页、登录、注册、登出、学生端、教师端。"""
import re
import json
import os
from html import escape
from flask import (
    jsonify, redirect, render_template, render_template_string, request, session, url_for, send_from_directory,
)


from core import app
from config import *
from db import *
from knowledge_base import *
from auth import *
from agent import *
from views.base import *

TEACHER_OPERATIONS_HEAD = '<link rel="stylesheet" href="/static/ui/teacher-operations.css">'
TEACHER_FINAL_HEAD = '<link rel="stylesheet" href="/static/ui/teacher-final.css">'

@app.route("/")
def index():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    if user["role"] == "teacher":
        return redirect(url_with_tab("teacher_dashboard"))
    return redirect(url_with_tab("student_collab"))


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, 'static'),
        'favicon.ico',
        mimetype='image/vnd.microsoft.icon'
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        login_key = request.form.get("login_key", "").strip()
        if login_key:
            from auth import verify_teacher_login_key, verify_participant_login_key, login_as_teacher_by_key, login_as_participant
            teacher_key = verify_teacher_login_key(login_key)
            if teacher_key:
                tab_token = login_as_teacher_by_key(teacher_key)
                if tab_token:
                    return redirect(url_for("teacher_dashboard", tab_token=tab_token))
            participant = verify_participant_login_key(login_key)
            if participant:
                tab_token = login_as_participant(participant)
                if tab_token:
                    return redirect(url_for("student_collab", tab_token=tab_token))
            error = "密钥无效或已停用"
        else:
            error = "请输入实验密钥"

    error_message = (
        f'<div class="login-field__message login-field__message--error" '
        f'id="login-key-message" role="alert" aria-live="polite">{error}</div>'
        if error
        else '<div class="login-field__message" id="login-key-message" aria-live="polite"></div>'
    )
    body = f"""
    <div class="login-page">
      <header class="login-brand">
        <img
          class="login-brand__icon"
          src="{url_for('static', filename='pic/icon.png')}"
          alt=""
        >
        <span class="login-brand__name">SSRL-ESP</span>
      </header>

      <main class="login-main">
        <section class="login-panel" aria-labelledby="login-title">
          <div class="login-eyebrow">
            <span class="login-eyebrow__line" aria-hidden="true"></span>
            <span>SSRL-ESP · 协同学习实验平台</span>
          </div>

          <h1 id="login-title" class="login-title">进入协同学习实验</h1>
          <p class="login-description">请输入实验管理员提供的专属密钥。系统将自动识别你的实验课次、小组及成员身份，并进入对应的协同学习空间。</p>

          <form class="login-form" method="post">
            <div class="login-field">
              <label class="login-field__label" for="login-key">实验密钥</label>
              <div class="login-input-wrap">
                <span class="login-input-icon" aria-hidden="true">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
                    <circle cx="8" cy="15" r="4"></circle>
                    <path d="m11 12 7.3-7.3a2.4 2.4 0 0 1 3.4 3.4L14.5 15"></path>
                    <path d="m16.5 6.5 2 2M14.2 8.8l2 2"></path>
                  </svg>
                </span>
                <input
                  class="login-input"
                  id="login-key"
                  name="login_key"
                  placeholder="请输入专属实验密钥"
                  autocomplete="off"
                  aria-describedby="login-key-help login-key-message"
                  aria-invalid="{'true' if error else 'false'}"
                  required
                >
              </div>
              <p class="login-field__hint" id="login-key-help">每位参与者使用独立密钥，仅用于本次实验的身份验证。</p>
              {error_message}
            </div>

            <button class="login-submit" type="submit" data-login-submit>
              <span class="login-submit__spinner" aria-hidden="true"></span>
              <span class="login-submit__text" data-login-label>进入实验空间</span>
              <span class="login-submit__arrow" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M5 12h13M13 7l5 5-5 5"></path>
                </svg>
              </span>
            </button>
          </form>

          <div class="login-help">
            <span class="login-help__line" aria-hidden="true"></span>
            <span class="login-help__icon" aria-hidden="true">?</span>
            <span>无法进入？请联系实验管理员确认密钥和实验课次。</span>
          </div>
        </section>
      </main>
    </div>
    <script>
      (() => {{
        const form = document.querySelector(".login-form");
        const submit = document.querySelector("[data-login-submit]");
        const label = document.querySelector("[data-login-label]");
        if (!form || !submit || !label) return;
        form.addEventListener("submit", () => {{
          submit.disabled = true;
          submit.setAttribute("aria-busy", "true");
          label.textContent = "正在验证…";
        }});
      }})();
    </script>
    """
    return render_template_string(
        f"""
        <!doctype html>
        <html lang="zh-CN">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>登录 - {APP_NAME}</title>
          <style>{BASE_CSS}</style>
          <link rel="stylesheet" href="/static/ui/design-tokens.css">
          <link rel="stylesheet" href="/static/ui/ui-primitives.css">
          <link rel="stylesheet" href="/static/ui/ui-motion.css">
          <link rel="stylesheet" href="/static/ui/auth-dashboard.css">
        </head>
        <body class="ui-page-background ui-bg-auth">{body}</body>
        </html>
        """
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    """Student registration is disabled in experiment mode."""
    html = render_template_string(
        f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>已禁用 - {APP_NAME}</title>
  <style>{BASE_CSS}</style>
  <link rel="stylesheet" href="/static/ui/design-tokens.css">
  <link rel="stylesheet" href="/static/ui/ui-primitives.css">
  <link rel="stylesheet" href="/static/ui/ui-motion.css">
  <link rel="stylesheet" href="/static/ui/teacher-final.css">
</head>
<body class="ui-page-background ui-bg-auth">
  <main class="restricted-state-shell">
    <section class="restricted-state-card">
      <header class="restricted-state-header">
        <h1>学生注册已禁用</h1>
        <p>当前系统仅支持实验密钥登录。此页面保留原 404 状态，不启用注册流程。</p>
      </header>
      <a class="ui-button ui-button-primary" href="{{{{ url_for('login') }}}}">返回登录</a>
    </section>
  </main>
</body>
</html>""",
    )
    return html, 404


@app.route("/logout")
def logout():
    token = get_tab_token_from_request()
    if token:
        execute("DELETE FROM client_sessions WHERE token=?", (token,))
    session.clear()
    return redirect(url_for("login"))


@app.route("/student")
@login_required("student")
def student_dashboard():
    """Redirect to the new three-column collaborative page."""
    user = current_user()
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="password")
    return redirect(url_for("student_collab", tab_token=tab_token))


@app.route("/student/questionnaire")
@login_required("student")
def student_questionnaire():
    """Redirect to new collab page (questionnaires handled inline)."""
    user = current_user()
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="password")
    mode = request.args.get("mode", "pre")
    phase = "pretest" if mode == "pre" else "posttest"
    return redirect(url_for("student_collab", phase=phase, tab_token=tab_token))
@app.route("/teacher")
@login_required("teacher")

def teacher_dashboard():
    """Teacher dashboard - module entry grid."""
    user = dict(current_user())
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="password")
        return redirect(url_for("teacher_dashboard", tab_token=tab_token))

    body = """{# SSRL-ESP Teacher 仪表盘 - Research Console Entry #}
{# This template provides the body content for the teacher dashboard page #}

<link rel="stylesheet" href="/static/ui/auth-dashboard.css">
<main class="container teacher-dashboard-page">
  <header class="dashboard-page-header">
    <div>
      <div class="dashboard-eyebrow">Teacher workspace</div>
      <h1>教师研究工作台</h1>
      <p>集中管理实验课次、学习过程分析与研究数据，在统一入口中进入各项教学与研究工具。</p>
    </div>
    <div class="dashboard-header-mark" aria-hidden="true">
      <svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <path d="M8 38V20l16-10 16 10v18"></path>
        <path d="M15 38V24h18v14M20 30h8"></path>
      </svg>
    </div>
  </header>

  <section class="dashboard-workspace ui-workspace" aria-labelledby="dashboard-modules-title">
    <div class="dashboard-workspace-head">
      <h2 id="dashboard-modules-title">工作入口</h2>
      <span>选择模块进入对应管理页面</span>
    </div>

    <div class="module-grid">
    <div class="module-card module-card-featured" role="link" tabindex="0" aria-label="进入实验控制" onclick="location.href='/teacher/session/control'" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}">
      <div class="module-icon">实验</div>
      <h3 class="module-title">实验控制</h3>
      <p class="module-desc">创建课次、选择角色与任务、开始/结束/归档</p>
      <span class="module-entry">进入 <span class="module-entry-arrow" aria-hidden="true">→</span></span>
    </div>

    <div class="module-card module-card-featured" role="link" tabindex="0" aria-label="进入参与度统计" onclick="location.href='/teacher/statistics'" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}">
      <div class="module-icon">参与</div>
      <h3 class="module-title">参与度统计</h3>
      <p class="module-desc">小组活跃度、发言分布、SSRL 事件统计</p>
      <span class="module-entry">进入 <span class="module-entry-arrow" aria-hidden="true">→</span></span>
    </div>

    <div class="module-card module-card-featured" role="link" tabindex="0" aria-label="进入情绪趋势" onclick="location.href='/teacher/emotion-trend'" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}">
      <div class="module-icon">情绪</div>
      <h3 class="module-title">情绪趋势</h3>
      <p class="module-desc">情绪打卡概览、群体情绪变化趋势</p>
      <span class="module-entry">进入 <span class="module-entry-arrow" aria-hidden="true">→</span></span>
    </div>

    <div class="module-card" role="link" tabindex="0" aria-label="进入历史查询与导出" onclick="location.href='/teacher/export'" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}">
      <div class="module-icon">导出</div>
      <h3 class="module-title">历史查询与导出</h3>
      <p class="module-desc">聊天记录、问卷数据、干预日志导出</p>
      <span class="module-entry">进入 <span class="module-entry-arrow" aria-hidden="true">→</span></span>
    </div>

    <div class="module-card" data-tone="warning" role="link" tabindex="0" aria-label="进入问卷管理" onclick="location.href='/teacher/questionnaire-admin'" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}">
      <div class="module-icon">问卷</div>
      <h3 class="module-title">问卷管理</h3>
      <p class="module-desc">创建、编辑、启用/停用调查问卷，查看作答统计</p>
      <span class="module-entry">进入 <span class="module-entry-arrow" aria-hidden="true">→</span></span>
    </div>

    <div class="module-card" data-tone="success" role="link" tabindex="0" aria-label="进入花名册" onclick="location.href='/teacher/roster'" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}">
      <div class="module-icon">花名</div>
      <h3 class="module-title">花名册</h3>
      <p class="module-desc">查看实名学生名单与研究分组信息</p>
      <span class="module-entry">进入 <span class="module-entry-arrow" aria-hidden="true">→</span></span>
    </div>
    </div>
  </section>
</main>
"""
    script = """"""
    return render_template_string(teacher_shell("\u6559\u5e08\u7aef - SSRL-ESP", body, script))

@app.route("/teacher/roster")
@login_required("teacher")
def teacher_roster():
    user = dict(current_user())
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="password")
        return redirect(url_for("teacher_roster", tab_token=tab_token))
    body = """
    <main class="container teacher-operations-page roster-management-page">
      <header class="ops-page-header">
        <h1>花名册</h1>
        <p>查看实名学生名单、参与编号、小组和实验条件，保留原有刷新和 CSV 导出入口。</p>
      </header>
      <div class="ops-workspace ui-workspace">
      <section class="ops-toolbar-card">
        <div>
          <h2>Student Management Workspace</h2>
          <p>加载、刷新和导出使用现有教师接口，不改变字段或返回结构。</p>
        </div>
        <div class="ops-inline-controls">
          <button class="btn small secondary" onclick="loadRoster()">刷新</button>
          <a class="btn small secondary" href="/export/roster.csv">导出CSV</a>
        </div>
      </section>
      <section class="card ops-table-card">
        <div class="card-hd">
          <h2>学生列表</h2>
        </div>
        <div class="card-bd">
          <div id="rosterLoading" class="ui-loading-state">加载中...</div>
          <div id="rosterTable" class="ops-table-scroll" style="display:none">
            <table class="ops-data-table roster-table" style="width:100%;border-collapse:collapse;font-size:13px">
              <thead>
                <tr style="border-bottom:2px solid var(--border);text-align:left">
                  <th style="padding:8px 12px">ID</th>
                  <th style="padding:8px 12px">用户名</th>
                  <th style="padding:8px 12px">真实姓名</th>
                  <th style="padding:8px 12px">参与编号</th>
                  <th style="padding:8px 12px">角色</th>
                  <th style="padding:8px 12px">小组</th>
                  <th style="padding:8px 12px">实验条件</th>
                  <th style="padding:8px 12px">注册时间</th>
                </tr>
              </thead>
              <tbody id="rosterBody"></tbody>
            </table>
          </div>
        </div>
      </section>
      </div>
    </main>
    """
    script = """<script src="/static/teacher/teacher-api.js"></script>
    <script>
      async function loadRoster() {
          document.getElementById('rosterLoading').style.display = 'grid';
          document.getElementById('rosterTable').style.display = 'none';
        try {
          const data = await fetchJSON('/api/teacher/roster');
          const users = data.users || [];
          const html = users.map(u => {
            const roleLabel = u.role === 'teacher' ? '教师' : u.role === 'agent' ? 'SERA' : '学生';
            const conditionLabel = u.condition === 'experiment' ? '实验组' : (u.condition === 'control' ? '对照组' : '-');
            return '<tr class="roster-row" style="border-bottom:1px solid var(--border-light)">' +
              '<td style="padding:8px 12px">' + u.id + '</td>' +
              '<td style="padding:8px 12px">' + escapeHtml(u.username || '') + '</td>' +
              '<td style="padding:8px 12px">' + escapeHtml(u.real_name || '') + '</td>' +
              '<td style="padding:8px 12px">' + escapeHtml(u.participant_code || '') + '</td>' +
              '<td style="padding:8px 12px"><span class="badge">' + roleLabel + '</span></td>' +
              '<td style="padding:8px 12px">' + escapeHtml(u.group_code || '-') + '</td>' +
              '<td style="padding:8px 12px">' + conditionLabel + '</td>' +
              '<td style="padding:8px 12px">' + escapeHtml(u.created_at || '') + '</td>' +
              '</tr>';
          }).join('');
          document.getElementById('rosterBody').innerHTML = html;
          document.getElementById('rosterLoading').style.display = 'none';
          document.getElementById('rosterTable').style.display = 'block';
        } catch (e) {
          console.error('花名册加载失败:', e);
          document.getElementById('rosterLoading').innerHTML = '<div class="evidence" style="color:var(--danger-text)">加载失败：' + escapeHtml(e.message || '未知错误') + '</div>';
        }
      }
      loadRoster();
    </script>
    """
    body = body.format(real_name=user.get("real_name") or user.get("username") or "")
    return render_template_string(teacher_shell("花名册 - SSRL-ESP", body, script, TEACHER_OPERATIONS_HEAD))


@app.route("/teacher/interventions")
@login_required("teacher")
def teacher_interventions():
    """干预策略管理页面 - 列出可用策略供教师选择推送."""
    user = dict(current_user())
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="password")
        return redirect(url_for("teacher_interventions", tab_token=tab_token))
    body = """
    <main class="container teacher-final-page interventions-page">
      <header class="final-page-header">
        <h1>干预策略</h1>
        <p>读取现有教师干预接口并展示真实加载、空状态或错误，不创建额外 API。</p>
      </header>
      <div class="final-workspace ui-workspace">
      <div class="card final-section-card">
        <div class="card-hd">
          <h2>可用干预策略</h2>
          <button class="btn small secondary" onclick="load干预记录()">刷新</button>
        </div>
        <div class="card-bd" id="interventionList"><div class="ui-loading-state">加载中...</div></div>
      </div>
      </div>
    </main>
    """
    script = """
    <script src="/static/teacher/teacher-api.js"></script>
    <script>
      async function load干预记录() {
        try {
          const data = await fetchJSON('/api/teacher/interventions');
          const items = data.interventions || [];
          if (!items.length) {
            document.getElementById('interventionList').innerHTML = '<div class="ui-empty-state">暂无干预策略。</div>';
            return;
          }
          const html = items.map(item => {
            return '<div class="group-card">' +
              '<div class="group-card-head"><div><div class="group-title">' + escapeHtml(item.title || '策略#' + item.id) + '</div>' +
              '<div class="muted" style="font-size:12px">ID: ' + item.id + '</div></div></div>' +
              '<div class="evidence">' + escapeHtml(item.message || '') + '</div>' +
              '</div>';
          }).join('');
          document.getElementById('interventionList').innerHTML = html;
        } catch (e) {
          document.getElementById('interventionList').innerHTML = '<div class="ui-error-state">加载失败：' + escapeHtml(e.message || '未知错误') + '</div>';
        }
      }
      load干预记录();
    </script>
    """
    body = body.format(real_name=user.get("real_name") or user.get("username") or "")
    return render_template_string(teacher_shell("干预策略 - SSRL-ESP", body, script, TEACHER_FINAL_HEAD))


# -----------------------------
# API 路由
# -----------------------------

# =========== Batch 7: T5 Agent 审计 page ===========
@app.route("/teacher/audit")
@login_required("teacher")
def teacher_audit():
    user = dict(current_user())
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="password")
        return redirect(url_for("teacher_audit", tab_token=tab_token))
    real_name = user.get("real_name") or user.get("username") or ""
    return render_template("teacher/audit.html", real_name=real_name)


AUDIT_BODY_TEMPLATE = """<div class="container">
  <div class="nav" style="margin-bottom:20px">
    <div class="nav-title">T5 Agent 审计</div>
    <div class="nav-user">
      <a class="btn small secondary" href="/teacher">仪表盘</a>
      <a class="btn small secondary" href="/logout">退出</a>
    </div>
  </div>

  <!-- Group / Session Selector -->
  <section class="card" style="margin-bottom:16px">
    <div class="card-hd"><h2>审计范围</h2></div>
    <div class="card-bd">
      <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:end">
        <label>小组
          <select id="audit-group-select" onchange="onAuditGroupChange()">
            <option value="">-- 请选择小组 --</option>
          </select>
        </label>
        <label>课次
          <select id="audit-session-select">
            <option value="">-- 请选择课次 --</option>
          </select>
        </label>
        <button class="btn small" onclick="loadAudit()">加载审计</button>
        <span id="audit-loading" class="muted" style="font-size:12px"></span>
      </div>
    </div>
  </section>

  <!-- 可追溯性警告 -->
  <div id="audit-traceability" style="margin-bottom:16px"></div>

  <!-- Main Audit Chain -->
  <div id="audit-chain"></div>

  <div id="audit-blinding-notice" style="display:none"></div>
</div>"""

AUDIT_SCRIPT_TEMPLATE = """<script src="/static/teacher/teacher-api.js"></script>
<script>
var _unblinded = true;

// Load groups on page init
window.addEventListener('DOMContentLoaded', function() {
  loadGroupOptions();
  loadSessionOptions();
});

async function loadGroupOptions() {
  try {
    var data = await window.fetchJSON('/api/teacher/groups?all=true');
    var sel = document.getElementById('audit-group-select');
    sel.innerHTML = '<option value=\"\">-- 请选择小组 --</option>' +
      (data.groups || []).map(function(g) {
        return '<option value=\"' + g.group_id + '\">' + window.escapeHtml(g.group_name || g.group_code || g.name || ('小组 #' + g.group_id)) + '</option>';
      }).join('');
  } catch (e) {
    console.error('load groups failed', e);
  }
}
async function loadSessionOptions() {
  try {
    var data = await window.fetchJSON('/api/teacher/sessions?all=true');
    var sel = document.getElementById('audit-session-select');
    sel.innerHTML = '<option value="">-- 请选择课次 --</option>' +
      (data.sessions || []).map(function(s) {
        return '<option value="' + s.id + '">课次 #' + s.id + ' | 第' + (s.session_no || '?') + ' 课时' + (s.session_role ? ' (' + window.escapeHtml(s.session_role) + ')' : '') + '</option>';
      }).join('');
  } catch (e) {
    console.error('load sessions failed', e);
  }
}

window.onAuditGroupChange = function() {
  loadSessionOptions();
};

window.loadAudit = async function() {
  var gid = parseInt(document.getElementById('audit-group-select').value);
  var sid = parseInt(document.getElementById('audit-session-select').value);
  if (!gid || !sid) {
    document.getElementById('audit-loading').textContent = '请选择小组和课次';
    return;
  }
  document.getElementById('audit-loading').textContent = '加载中...';
  try {
    var blinded = false;
    var data = await window.fetchJSON('/api/teacher/group/' + gid + '/agent-audit?session_id=' + sid + '&blinded=' + blinded);
    renderAuditChain(data, gid, sid);
    document.getElementById('audit-loading').textContent = '';
  } catch (e) {
    document.getElementById('audit-loading').textContent = '加载失败: ' + window.escapeHtml(e.message);
  }
};

function renderAuditChain(data, groupId, sessionId) {
  var chain = document.getElementById('audit-chain');
  var html = '';

  // 可追溯性警告
  var warnings = data.traceability_warnings || [];
  var traceDiv = document.getElementById('audit-traceability');
  if (warnings.length) {
    traceDiv.innerHTML = '<section class=\"card\" style=\"border-color:var(--warning-border);margin-bottom:16px\">' +
      '<div class=\"card-hd\" style=\"background:var(--warning-soft)\"><h2 style=\"font-size:13px;color:var(--warning-text)\">可追溯性警告</h2></div>' +
      '<div class=\"card-bd\" style=\"padding:12px 16px\">' +
      warnings.map(function(w) { return '<div style=\"font-size:12px;color:var(--text-secondary);margin-bottom:4px\">&bull; ' + window.escapeHtml(w) + '</div>'; }).join('') +
      '</div></section>';
  } else if (false) {
    traceDiv.innerHTML = '';
  }

  // Direct-view audit: no unblind control is needed.
  var blindNotice = document.getElementById('audit-blinding-notice');
  if (blindNotice) blindNotice.innerHTML = '';

  // 1. 检测器输出
  var detectors = data.detector_outputs || [];
  html += '<section class=\"card\" style=\"margin-bottom:16px\">' +
    '<div class=\"card-hd\"><h2>检测器输出</h2><span class=\"badge\">' + detectors.length + '</span></div>' +
    '<div class=\"card-bd\" style=\"padding:12px 16px\">';
  if (detectors.length) {
    html += '<div style=\"display:grid;gap:8px\">' + detectors.map(function(d) {
      var items = [];
      if (d.state_code) items.push('<span class=\"badge\">' + window.escapeHtml(d.state_code) + '</span>');
      if (d.fused_state_code) items.push('<span class=\"badge\">' + window.escapeHtml(d.fused_state_code) + '</span>');
      if (d.risk_level != null) items.push('<span class=\"badge ' + window.riskClass(d.risk_level) + '\">Risk ' + d.risk_level + '</span>');
      if (d.confidence != null) items.push('<span class=\"badge\">Conf ' + d.confidence + '</span>');
      if (d.state_score != null) items.push('<span class=\"badge\">Score ' + d.state_score + '</span>');
      return '<div class=\"evidence\" style=\"background:var(--surface);font-size:12px\">' +
        '<div style=\"display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px\">' + items.join('') + '</div>' +
        (d.created_at ? '<div class=\"muted\" style=\"font-size:11px\">' + window.formatDt(d.created_at) + '</div>' : '') +
        '</div>';
    }).join('') + '</div>';
  } else {
    html += '<div class=\"muted\" style=\"font-size:12px\">无检测器输出。</div>';
  }
  html += '</div></section>';

  // 2. 门控记录
  var gates = data.gate_records || [];
  html += '<section class=\"card\" style=\"margin-bottom:16px\">' +
    '<div class=\"card-hd\"><h2>门控记录</h2><span class=\"badge\">' + gates.length + '</span></div>' +
    '<div class=\"card-bd\" style=\"padding:12px 16px\">';
  if (gates.length) {
    html += '<div style=\"display:grid;gap:8px\">' + gates.map(function(g) {
      var items = [];
      if (g.should_intervene != null) items.push('<span class=\"badge ' + (g.should_intervene ? '' : 'low') + '\">' + (g.should_intervene ? '干预' : '跳过') + '</span>');
      if (g.target) items.push('<span style=\"font-size:12px\">Target: ' + window.escapeHtml(g.target) + '</span>');
      if (g.priority != null) items.push('<span class=\"badge\">P' + g.priority + '</span>');
      if (g.strategy_category) items.push('<span style=\"font-size:12px\">Cat: ' + window.escapeHtml(g.strategy_category) + '</span>');
      return '<div class=\"evidence\" style=\"background:var(--surface);font-size:12px\">' +
        '<div style=\"display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px\">' + items.join('') + '</div>' +
        (g.decision_reason ? '<div style=\"font-size:11px;color:var(--text-secondary)\">' + window.escapeHtml(g.decision_reason) + '</div>' : '') +
        (g.suppressed_reason ? '<div style=\"font-size:11px;color:var(--danger-text)\">Suppressed: ' + window.escapeHtml(g.suppressed_reason) + '</div>' : '') +
        (g.created_at ? '<div class=\"muted\" style=\"font-size:11px\">' + window.formatDt(g.created_at) + '</div>' : '') +
        '</div>';
    }).join('') + '</div>';
  } else {
    html += '<div class=\"muted\" style=\"font-size:12px\">无门控记录。</div>';
  }
  html += '</div></section>';

  // 3. 干预记录
  var interventions = data.interventions || [];
  html += '<section class=\"card\" style=\"margin-bottom:16px\">' +
    '<div class=\"card-hd\"><h2>干预记录</h2><span class=\"badge\">' + interventions.length + '</span></div>' +
    '<div class=\"card-bd\" style=\"padding:12px 16px\">';
  if (interventions.length) {
    html += '<div style=\"display:grid;gap:10px\">' + interventions.map(function(iv) {
      var items = [];
      if (iv.title) items.push('<strong>' + window.escapeHtml(iv.title) + '</strong>');
      if (iv.strategy_type) items.push('<span class=\"badge\">' + window.escapeHtml(iv.strategy_type) + '</span>');
      if (iv.intervention_index != null) items.push('<span class=\"badge\">#' + iv.intervention_index + '</span>');
      var linked = iv.linked_run;
      return '<div class=\"evidence\" style=\"background:var(--surface);font-size:12px\">' +
        '<div style=\"display:flex;gap:6px;flex-wrap:wrap;margin-bottom:4px\">' + items.join('') + '</div>' +
        (iv.message ? '<div style=\"margin:4px 0;padding:6px 8px;border-radius:6px;background:var(--primary-soft);font-size:12px;white-space:pre-wrap\">' + window.escapeHtml(iv.message.length > 200 ? iv.message.substring(0,200) + '...' : iv.message) + '</div>' : '') +
        (linked ? '<div style=\"font-size:11px;color:var(--text-muted)\">Run #' + linked.id + ' (' + window.escapeHtml(linked.status || '?') + ', conf=' + (linked.confidence != null ? linked.confidence : 'N/A') + ')' : '') +
        (linked && linked.detected_state ? ' | state: ' + window.escapeHtml(linked.detected_state) : '') + '</div>' +
        (iv.created_at ? '<div class=\"muted\" style=\"font-size:11px;margin-top:4px\">' + window.formatDt(iv.created_at) + '</div>' : '') +
        '</div>';
    }).join('') + '</div>';
  } else {
    html += '<div class=\"muted\" style=\"font-size:12px\">无干预记录。</div>';
  }
  html += '</div></section>';

  // 4. 采纳记录
  var uptakes = data.uptake || [];
  html += '<section class=\"card\" style=\"margin-bottom:16px\">' +
    '<div class=\"card-hd\"><h2>采纳记录</h2><span class=\"badge\">' + uptakes.length + '</span></div>' +
    '<div class=\"card-bd\" style=\"padding:12px 16px\">';
  if (uptakes.length) {
    html += '<div style=\"display:grid;gap:10px\">' + uptakes.map(function(u) {
      var type = u.manual_uptake_type || u.auto_uptake_type || 'pending';
      var badgeClass = type === 'adopted' || type === 'adapted' ? '' : (type === 'acknowledged' || type === 'discussed' ? 'low' : 'mid');
      return '<div class=\"evidence\" style=\"background:var(--surface);font-size:12px\">' +
        '<div style=\"display:flex;gap:6px;align-items:center;flex-wrap:wrap\">' +
        '<span class=\"badge ' + badgeClass + '\">' + window.escapeHtml(type) + '</span>' +
        (u.target_ssrl_behavior ? '<span style=\"font-size:11px;color:var(--text-secondary)\">行为：' + window.escapeHtml(u.target_ssrl_behavior) + '</span>' : '') +
        (u.target_behavior_occurred ? '<span class=\"badge low\">Occurred</span>' : '') +
        '</div>' +
        (u.response_latency_seconds != null ? '<div style=\"font-size:11px;color:var(--text-muted);margin-top:4px\">延迟：' + (u.response_latency_seconds || 0) + '秒 | 回复：' + (u.response_count_2min || 0) + ' | 参与者：' + (u.participant_count_2min || 0) + '</div>' : '') +
        (u.corrected_by ? '<div style=\"font-size:11px;color:var(--text-muted)\">教师校正：' + window.escapeHtml(u.manual_uptake_type || '') + (u.corrected_at ? ' (' + window.formatDt(u.corrected_at) + ')' : '') + '</div>' : '') +
        '<div style=\"margin-top:4px\"><button class=\"btn small secondary\" onclick=\"show采纳记录Correction(' + u.id + ',' + groupId + ',' + sessionId + ')\">校正类型</button></div>' +
        '</div>';
    }).join('') + '</div>';
  } else {
    html += '<div class=\"muted\" style=\"font-size:12px\">无采纳记录。</div>';
  }
  html += '</div></section>';

  // 5. 自主调节事件
  var autoReg = data.autonomous_regulation_events || [];
  html += '<section class=\"card\" style=\"margin-bottom:16px\">' +
    '<div class=\"card-hd\"><h2>自主调节事件</h2><span class=\"badge\">' + autoReg.length + '</span></div>' +
    '<div class=\"card-bd\" style=\"padding:12px 16px\">';
  if (autoReg.length) {
    html += '<div style=\"display:grid;gap:8px\">' + autoReg.map(function(e) {
      return '<div class=\"evidence\" style=\"background:var(--surface);font-size:12px\">' +
        '<div style=\"display:flex;gap:6px;flex-wrap:wrap;margin-bottom:4px\">' +
        '<span class=\"badge\">' + window.escapeHtml(e.event_type) + '</span>' +
        (e.confidence != null ? '<span class=\"badge\">Conf ' + e.confidence + '</span>' : '') +
        (e.detected_by ? '<span style=\"font-size:11px;color:var(--text-muted)\">Detected by: ' + window.escapeHtml(e.detected_by) + '</span>' : '') +
        '</div>' +
        (e.note ? '<div style=\"font-size:11px\">' + window.escapeHtml(e.note) + '</div>' : '') +
        (e.created_at ? '<div class=\"muted\" style=\"font-size:11px;margin-top:4px\">' + window.formatDt(e.created_at) + '</div>' : '') +
        '</div>';
    }).join('') + '</div>';
  } else {
    html += '<div class=\"muted\" style=\"font-size:12px\">无自主调节事件。</div>';
  }
  html += '</div></section>';

  chain.innerHTML = html;
}

window.auditDirectViewNoop = function() {
  return false;
};

window.show采纳记录Correction = function(uptakeId, groupId, sessionId) {
  var type = prompt('Enter uptake type (ignored/acknowledged/discussed/adopted/adapted/rejected):');
  if (!type || !type.trim()) return;
  var reason = prompt('Enter reason (optional):');
  window.fetchJSON('/api/teacher/group/' + groupId + '/agent-audit?session_id=' + sessionId + '&blinded=true').then(function(data) {
    var uptakes = data.uptake || [];
    var found = uptakes.filter(function(u) { return u.id === uptakeId; });
    if (found.length && found[0].intervention_id) {
      return window.fetchJSON('/api/teacher/intervention/' + found[0].intervention_id + '/manual-uptake', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({manual_uptake_type: type.trim(), reason: reason || null})
      });
    }
    throw new Error('采纳记录 record not found');
  }).then(function() {
    loadAudit();
  }).catch(function(e) {
    alert('校正失败：' + e.message);
  });
};
</script>"""


@app.route("/teacher/export")
@login_required("teacher")
def teacher_export_page():
    """Full-scope research export center."""
    from flask import render_template_string
    from views.base import teacher_shell
    from auth import login_required, current_user, get_tab_token_from_request, redirect, url_for
    user = dict(current_user())
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="password")
        return redirect(url_for("teacher_export_page", tab_token=tab_token))
    real_name = user.get("real_name") or user.get("username") or ""
    body = """<main class="container teacher-operations-page export-center-page">
  <header class="ops-page-header">
    <h1>数据导出</h1>
    <p>所有导出均包含系统当前保存的全部课次和全部小组数据，请妥善保存。</p>
  </header>
  <div class="ops-workspace ui-workspace">
  <section class="card ops-export-card">
    <div class="card-hd"><h2>Export Center</h2></div>
    <div class="card-bd">
      <div class="ops-export-grid">
        <div class="ops-export-note">
          <strong>数据范围</strong>
          <span class="muted">全量、非盲化研究数据；不应用课次、小组、任务或时间筛选。</span>
          <strong>文件说明</strong>
          <span class="muted">所有接口均返回按“课次 → 小组 → 文件”组织的 ZIP；只有问卷进入个人目录。</span>
        </div>
        <div>
      <div class="export-links ops-export-links">
        <a class="btn secondary small" href="/export/messages">导出聊天记录</a>
        <a class="btn secondary small" href="/export/state-assessments">导出状态判断</a>
        <a class="btn secondary small" href="/export/strategy-pipeline">导出策略流水</a>
        <a class="btn secondary small" href="/export/interventions">导出已发布介入</a>
        <a class="btn secondary small" href="/export/participation">导出参与度摘要</a>
        <a class="btn secondary small" href="/export/emotion-checkins">导出情绪签到</a>
        <a class="btn secondary small" href="/export/emotion-feedback">导出群体情绪反馈</a>
        <a class="btn secondary small" href="/export/help-requests">导出主动求助</a>
        <a class="btn secondary small" href="/export/deliverables">导出最终成果</a>
        <a class="btn secondary small" href="/export/questionnaires">导出问卷原始数据</a>
      </div>
      <div class="ops-export-actions">
        <a class="btn small ops-primary-download" href="/export/all">导出全部研究数据</a>
        <span class="muted" style="font-size:12px">CSV 使用 UTF-8 BOM，最终成果使用 Markdown。</span>
      </div>
        </div>
      </div>
    </div>
  </section>
  </div>

</main>
<script src="/static/teacher/teacher-api.js"></script>"""
    return render_template_string(teacher_shell("数据导出 - SSRL-ESP", body, "", TEACHER_OPERATIONS_HEAD))

@app.route("/teacher/history")
@login_required("teacher")
def teacher_history():
    """Retired history query page; exports are the only supported data access."""
    tab_token = get_tab_token_from_request()
    values = {"tab_token": tab_token} if tab_token else {}
    return redirect(url_for("teacher_export_page", **values))


@app.route("/teacher/session/control")
@login_required("teacher")
def teacher_session_control():
    """T1: Session control page."""
    user = dict(current_user())
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="password")
        return redirect(url_for("teacher_session_control", tab_token=tab_token))
    body = render_template("teacher/session_control.html")
    script = ''
    return render_template_string(teacher_shell("\u5b9e\u9a8c\u63a7\u5236 - SSRL-ESP", body, script, TEACHER_OPERATIONS_HEAD))

@app.route("/teacher/statistics")
@login_required("teacher")
def teacher_statistics():
    """T3: Participation statistics page with global status bar."""
    from views.base import teacher_shell
    from auth import login_required, current_user, get_tab_token_from_request, redirect, url_for
    user = dict(current_user())
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="password")
        return redirect(url_for("teacher_statistics", tab_token=tab_token))
    real_name = user.get("real_name") or user.get("username") or ""

    body = """<div class="container">
  <div class="nav" style="margin-bottom:20px">
    <div class="nav-title">T3 参与度统计</div>
    <div class="nav-user">
      <span>""" + real_name + """</span>
      <a class="btn small secondary" href="/teacher">仪表盘</a>
      <a class="btn small secondary" href="/teacher/emotion-trend">情绪趋势</a>
      <a class="btn small secondary" href="/logout">退出</a>
    </div>
  </div>

  <main class="teacher-analytics-page participation-analytics-page">
    <header class="analytics-page-header">
      <div>
        <span class="analytics-eyebrow">Teacher Analytics Workspace</span>
        <h1>参与度统计</h1>
        <p>按课次与小组审阅成员参与分布、详细数据和时间趋势。</p>
      </div>
      <span class="analytics-page-badge">只读分析</span>
    </header>

    <div class="analytics-workspace ui-workspace">
  <!-- Filter Bar -->
  <div class="card analytics-filter-card">
    <div class="card-hd"><h2>筛选条件</h2></div>
    <div class="card-bd">
      <div class="analytics-filter-grid">
        <div>
          <label class="text-sm muted">Group</label>
          <select id="group-select"><option value="">-- 选择 Group --</option></select>
        </div>
        <div>
          <label class="text-sm muted">Session</label>
          <select id="session-select"><option value="">-- 选择 Session --</option></select>
        </div>
        <div>
          <label class="text-sm muted">Timeline Metric</label>
          <select id="timeline-metric">
            <option value="message_count">消息数</option>
            <option value="char_count">字符数</option>
            <option value="active_minutes">活跃分钟数</option>
          </select>
        </div>
        <div>
          <label class="text-sm muted">窗口（分钟）</label>
          <input type="number" id="window-minutes" value="5" min="1" step="1">
        </div>
      </div>
      <div style="margin-top:8px">
        <span class="muted" style="font-size:11px" id="last-updated"></span>
      </div>
    </div>
  </div>

  <!-- Group Summary -->
  <section class="card analytics-metric-card">
    <div class="card-hd"><h2>小组统计摘要</h2></div>
    <div class="card-bd" id="group-summary-area">
      <div class="evidence">请选择 Group</div>
    </div>
  </section>

  <!-- Member Bars -->
  <div class="analytics-main-grid analytics-main-grid-balanced">
    <section class="card analytics-chart-card">
      <div class="card-hd"><h2>成员消息数</h2></div>
      <div class="card-bd" id="member-bars-area">
        <div class="evidence">暂无参与度数据</div>
      </div>
    </section>
    <section class="card analytics-chart-card">
      <div class="card-hd"><h2>消息占比</h2></div>
      <div class="card-bd" id="share-bars-area">
        <div class="evidence">暂无参与度数据</div>
      </div>
    </section>
  </div>

  <!-- Detail Table -->
  <section class="card analytics-table-card">
    <div class="card-hd"><h2>详细数据</h2></div>
    <div class="card-bd" id="detail-table-area">
      <div class="evidence">暂无参与度数据</div>
    </div>
  </section>

  <!-- Timeline -->
  <section class="card analytics-chart-card analytics-chart-card-wide">
    <div class="card-hd"><h2>参与度时间线</h2></div>
    <div class="card-bd" id="timeline-trend-area">
      <div class="evidence">暂无参与度数据</div>
    </div>
  </section>
    </div>
  </main>
</div>"""

    script = """<script src="/static/teacher/teacher-api.js"></script>
<script src="/static/teacher/participation-statistics.js"></script>
<script>window.initParticipationStats();</script>"""

    return render_template_string(
        teacher_shell(
            "参与度统计 - SSRL-ESP",
            body,
            script,
            head='<link rel="stylesheet" href="/static/ui/teacher-analytics.css">',
        )
    )


@app.route("/teacher/emotion-trend")
@login_required("teacher")
def teacher_emotion_trend():
    """T4: Emotion trend page with global status bar."""
    from views.base import teacher_shell
    from auth import login_required, current_user, get_tab_token_from_request, redirect, url_for
    user = dict(current_user())
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="password")
        return redirect(url_for("teacher_emotion_trend", tab_token=tab_token))
    real_name = user.get("real_name") or user.get("username") or ""

    body = """<div class="container">
  <div class="nav" style="margin-bottom:20px">
    <div class="nav-title">T4 情绪趋势</div>
    <div class="nav-user">
      <span>""" + real_name + """</span>
      <a class="btn small secondary" href="/teacher">仪表盘</a>
      <a class="btn small secondary" href="/teacher/statistics">参与度统计</a>
      <a class="btn small secondary" href="/logout">退出</a>
    </div>
  </div>

  <style>
    .emotion-filter-grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); align-items:end; }
    .emotion-filter-actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    .emotion-review-note { margin-top:8px; padding:8px 12px; background:var(--surface-soft); border:1px solid var(--border-light); border-radius:var(--radius-sm); font-size:12px; color:var(--text-secondary); }
    .emotion-overview { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    .emotion-grid { display:grid; grid-template-columns:minmax(0,1.35fr) minmax(320px,.65fr); gap:12px; align-items:start; }
    .emotion-side { display:grid; gap:12px; }
    .emotion-toolbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    .emotion-toolbar input, .emotion-toolbar select { min-height:32px; }
    .emotion-role-filter.active { background:var(--primary); color:#fff; border-color:var(--primary-border); }
    .emotion-timeline { position:relative; min-height:190px; }
    .emotion-axis { position:relative; height:40px; margin:4px 0 14px; border-top:1px solid var(--border); }
    .emotion-tick { position:absolute; top:-5px; width:1px; height:10px; background:var(--border); }
    .emotion-tick.minor { top:-3px; height:6px; background:var(--border-light); }
    .emotion-tick span { position:absolute; top:13px; left:50%; transform:translateX(-50%); color:var(--text-muted); font-size:11px; white-space:nowrap; }
    .emotion-tick.minor span { display:none; }
    .emotion-state-band { position:relative; height:86px; border:1px solid var(--border-light); border-radius:var(--radius-sm); overflow:hidden; background:var(--surface-soft); }
    .emotion-state-band.segmented { height:auto; display:grid; gap:6px; padding:6px; overflow:visible; }
    .emotion-state-lane { display:grid; grid-template-columns:98px minmax(0,1fr); gap:8px; align-items:center; min-height:32px; }
    .emotion-lane-name { display:flex; align-items:center; gap:5px; min-width:0; color:var(--text-secondary); font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .emotion-lane-body { position:relative; min-height:30px; border:1px solid var(--border-light); border-radius:6px; background:rgba(255,255,255,.44); overflow:hidden; }
    .emotion-state-segment { position:absolute; top:4px; height:22px; display:flex; align-items:center; justify-content:center; padding:3px 6px; color:white; font-size:11px; font-weight:700; text-align:center; border-right:1px solid rgba(255,255,255,.72); border-radius:4px; overflow:hidden; cursor:pointer; }
    .emotion-state-segment.silence { border:1px dashed rgba(255,255,255,.82); }
    .emotion-state-segment span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .emotion-state-event { position:absolute; height:19px; display:flex; align-items:center; justify-content:center; padding:0 6px; border:1px solid rgba(255,255,255,.72); border-radius:4px; color:white; font-size:10px; font-weight:700; text-align:center; overflow:hidden; cursor:pointer; }
    .emotion-state-event span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .emotion-agent-lane { position:relative; height:46px; margin-top:10px; border:1px dashed var(--border); border-radius:var(--radius-sm); background:rgba(255,255,255,.38); }
    .emotion-lane-label { position:absolute; left:10px; top:50%; transform:translateY(-50%); color:var(--text-muted); font-size:12px; pointer-events:none; }
    .emotion-agent-marker { position:absolute; top:7px; width:12px; height:30px; transform:translateX(-50%); border:2px solid var(--primary); border-radius:8px; background:var(--primary-soft); cursor:pointer; }
    .emotion-agent-marker.ordinary { background:rgba(255,255,255,.74); }
    .emotion-agent-marker.formal { border-color:var(--primary); background:var(--primary-soft); }
    .emotion-agent-marker.help { border-color:var(--warning-text); background:var(--warning-soft); }
    .emotion-agent-marker.auto { border-color:var(--success-text); background:var(--success-soft); }
    .emotion-legend { display:flex; gap:8px 12px; flex-wrap:wrap; margin-top:10px; }
    .emotion-legend-item { display:flex; align-items:center; gap:5px; color:var(--text-secondary); font-size:11px; }
    .emotion-swatch { width:10px; height:10px; border-radius:2px; }
    .emotion-messages { display:grid; gap:8px; }
    .emotion-message { display:grid; grid-template-columns:56px 72px minmax(84px,112px) minmax(108px,140px) minmax(0,1fr); gap:10px; align-items:start; padding:10px; border:1px solid var(--border-light); border-radius:var(--radius-sm); background:rgba(255,255,255,.46); }
    .emotion-message.agent { border-color:var(--primary-border); background:var(--primary-soft); }
    .emotion-message .seq, .emotion-message .time { color:var(--text-muted); font-size:12px; font-variant-numeric:tabular-nums; }
    .emotion-message .speaker { font-weight:700; color:var(--text); }
    .emotion-message .content { line-height:1.55; word-break:break-word; }
    .emotion-chip { display:inline-flex; align-items:center; max-width:100%; padding:4px 7px; border-radius:999px; color:white; font-size:11px; font-weight:700; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .emotion-chip.observing { color:#31577b; background:#e8f2fb !important; border:1px dashed #8eb3d5; }
    .emotion-state-empty { display:inline-block; min-width:22px; min-height:20px; }
    .emotion-message.status-unclassified { background:rgba(255,255,255,.34); }
    .emotion-bars { display:grid; gap:9px; }
    .emotion-bar-row { display:grid; grid-template-columns:76px minmax(0,1fr) 34px; gap:8px; align-items:center; font-size:12px; }
    .emotion-bar-row.distribution-row { grid-template-columns:76px minmax(0,1fr) 34px; }
    .distribution-detail { grid-column:1 / -1; color:var(--text-muted); font-size:11px; line-height:1.35; }
    .emotion-bar-track { height:12px; border-radius:999px; border:1px solid var(--border-light); background:var(--surface-soft); overflow:hidden; }
    .emotion-bar-fill { height:100%; border-radius:999px; }
    .participation-summary { display:grid; gap:7px; margin-bottom:10px; }
    .participation-minute-row { display:grid; grid-template-columns:58px minmax(0,1fr) 34px; gap:8px; align-items:center; font-size:12px; }
    .participation-minute-time, .participation-minute-count { color:var(--text-muted); font-variant-numeric:tabular-nums; }
    .participation-minute-track { display:flex; height:18px; border:1px solid var(--border-light); border-radius:6px; background:var(--surface-soft); overflow:hidden; }
    .participation-minute-segment { min-width:4px; height:100%; cursor:pointer; }
    .participation-minute-empty { width:100%; height:100%; background:rgba(255,255,255,.34); }
    .participation-legend { display:flex; gap:6px 10px; flex-wrap:wrap; margin-top:10px; }
    .participation-legend-item { display:flex; gap:5px; align-items:center; color:var(--text-secondary); font-size:11px; }
    .emotion-interventions { display:grid; gap:8px; }
    .emotion-intervention { width:100%; padding:10px; border:1px solid var(--border-light); border-radius:var(--radius-sm); background:rgba(255,255,255,.46); font:inherit; font-size:12px; line-height:1.45; text-align:left; color:var(--text); cursor:pointer; }
    .emotion-intervention strong { display:block; margin-bottom:4px; color:var(--text); }
    .emotion-empty { padding:14px; border:1px dashed var(--border); border-radius:var(--radius-sm); color:var(--text-muted); text-align:center; font-size:13px; }
    @media (max-width: 980px) { .emotion-grid { grid-template-columns:1fr; } }
    @media (max-width: 680px) {
      .emotion-message { grid-template-columns:46px minmax(0,1fr); }
      .emotion-message .time, .emotion-message .speaker, .emotion-message .state-cell, .emotion-message .content { grid-column:2; }
      .emotion-bar-row { grid-template-columns:70px minmax(0,1fr) 30px; }
      .emotion-state-lane { grid-template-columns:1fr; gap:4px; }
      .emotion-lane-name { white-space:normal; }
    }
  </style>

  <main class="teacher-analytics-page emotion-analytics-page">
    <header class="analytics-page-header">
      <div>
        <span class="analytics-eyebrow">Teacher Analytics Workspace</span>
        <h1>情绪趋势</h1>
        <p>沿时间线审阅协作状态、消息流、参与度和 Agent 介入。</p>
      </div>
      <span class="analytics-page-badge">过程审阅</span>
    </header>

    <div class="analytics-workspace ui-workspace">
  <div class="card analytics-filter-card">
    <div class="card-hd">
      <h2>筛选条件</h2>
      <span class="muted" style="font-size:11px" id="last-updated"></span>
    </div>
    <div class="card-bd">
      <div class="emotion-filter-grid">
        <div>
          <label class="text-sm muted">Group</label>
          <select id="group-select"><option value="">-- 选择小组 --</option></select>
        </div>
        <div>
          <label class="text-sm muted">Session</label>
          <select id="session-select"><option value="">-- 选择课次 --</option></select>
        </div>
        <div>
          <label class="text-sm muted">显示间隔（分钟）</label>
          <input type="number" id="window-minutes" value="1" min="1" step="1">
        </div>
        <div class="emotion-filter-actions">
          <button class="btn small" type="button" onclick="window.loadData()">刷新</button>
        </div>
      </div>
      <div id="legacy-warning" style="display:none;margin-top:8px;padding:8px 12px;background:var(--warning-soft);border:1px solid var(--warning-border);border-radius:var(--radius-sm);font-size:12px;color:var(--warning-text)">
        &#9888; 部分数据来自旧版格式，可能不完整。
      </div>
      <div class="emotion-review-note">
        本页按时间线对齐小组消息、规则/融合状态与 Agent 介入，用于审阅协作过程变化，不等同于学生个体情绪诊断。
      </div>
    </div>
  </div>

  <section class="card analytics-metric-card">
    <div class="card-hd">
      <h2>审阅概览</h2>
    </div>
    <div class="card-bd">
      <div class="emotion-overview analytics-metric-grid" id="review-summary"></div>
    </div>
  </section>

  <section class="card analytics-chart-card analytics-chart-card-wide">
    <div class="card-hd">
      <h2>协作状态时间轴</h2>
    </div>
    <div class="card-bd" id="timeline-area">
      <div class="emotion-empty">暂无数据</div>
    </div>
  </section>

  <div class="emotion-grid">
    <section class="card analytics-data-card">
      <div class="card-hd">
        <h2>消息流</h2>
        <div class="emotion-toolbar">
          <button class="btn small secondary emotion-role-filter" type="button" data-role="all">全部</button>
          <button class="btn small secondary emotion-role-filter" type="button" data-role="student">学生</button>
          <button class="btn small secondary emotion-role-filter" type="button" data-role="agent">Agent</button>
          <select id="state-filter"><option value="all">全部状态</option></select>
          <input id="message-search" type="search" placeholder="搜索消息">
          <span class="badge" id="message-count-badge">0 条</span>
        </div>
      </div>
      <div class="card-bd">
        <div class="emotion-messages" id="message-flow-area">
          <div class="emotion-empty">暂无数据</div>
        </div>
      </div>
    </section>

    <aside class="emotion-side">
      <section class="card analytics-summary-card">
        <div class="card-hd"><h2>参与度</h2></div>
        <div class="card-bd" id="participation-area">
          <div class="emotion-empty">暂无数据</div>
        </div>
      </section>
      <section class="card analytics-summary-card">
        <div class="card-hd"><h2>状态分布</h2></div>
        <div class="card-bd" id="state-distribution-area">
          <div class="emotion-empty">暂无数据</div>
        </div>
      </section>
      <section class="card analytics-summary-card">
        <div class="card-hd"><h2>Agent 介入</h2></div>
        <div class="card-bd" id="intervention-area">
          <div class="emotion-empty">暂无数据</div>
        </div>
      </section>
    </aside>
  </div>
    </div>
  </main>
</div>"""

    script = """<script src="/static/teacher/teacher-api.js"></script>
<script src="/static/teacher/emotion-trend.js"></script>
<script>window.initEmotionTrendPage();</script>"""

    return render_template_string(
        teacher_shell(
            "情绪趋势 - SSRL-ESP",
            body,
            script,
            head='<link rel="stylesheet" href="/static/ui/teacher-analytics.css">',
        )
    )


@app.route("/teacher/questionnaire-admin")
@login_required("teacher")
def teacher_questionnaire_admin():
    """Questionnaire admin page."""
    from views.base import teacher_shell
    from auth import login_required, current_user, get_tab_token_from_request, redirect, url_for
    user = dict(current_user())
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="password")
        return redirect(url_for("teacher_questionnaire_admin", tab_token=tab_token))
    real_name = user.get("real_name") or user.get("username") or ""

    # Read the template file
    tpl_path = os.path.join(os.path.dirname(__file__), "..", "templates", "teacher", "questionnaire_admin.html")
    with open(tpl_path, "r", encoding="utf-8") as ft:
        tpl_html = ft.read()

    # Remove Jinja2 comment line
    lines = tpl_html.split("\n")
    lines = [l for l in lines if not l.strip().startswith("{#") and not l.strip().endswith("#}")]
    
    content_body = "\n".join(lines)

    # Split at first <script tag to separate HTML from JS
    script_idx = content_body.find('<script')
    if script_idx >= 0:
        html_part = content_body[:script_idx]
        js_part = content_body[script_idx:]
    else:
        html_part = content_body
        js_part = ''

    # Remove the templates container opening and nav section.
    container_nav_marker = '<div class="container">\n  <div class="nav" style="margin-bottom:20px">'
    nav_end_marker = '  </div>\n\n  <div style="display:grid'
    sn = html_part.find(container_nav_marker)
    en = html_part.find(nav_end_marker)
    if sn >= 0 and en >= 0:
        en = en + len('  </div>')
        html_part = html_part[:sn] + html_part[en:]

    body = """<main class="container teacher-final-page questionnaire-admin-page">
  <header class="final-page-header">
    <h1>问卷管理</h1>
    <p>维护自定义问卷和问卷包，保留创建、编辑、启停、排序和删除入口。</p>
  </header>
  <div class="final-workspace ui-workspace">
""" + html_part + """
  </div>
</main>"""

    script = js_part

    return render_template_string(teacher_shell("问卷管理 - SSRL-ESP", body, script, TEACHER_FINAL_HEAD))


@app.route("/teacher/questionnaire-management")
@login_required("teacher")
def teacher_questionnaire_management():
    """Teacher questionnaire management with tabs: fixed library, publications, completion, export."""
    user = dict(current_user())
    tab_token = get_tab_token_from_request()
    if not tab_token:
        tab_token = create_client_session(user["id"], user["role"], login_method="password")
        return redirect(url_for("teacher_questionnaire_management", tab_token=tab_token))
    real_name = user.get("real_name") or user.get("username") or ""

    body = """
<main class="container teacher-final-page questionnaire-management-page">
  <header class="final-page-header">
    <h1>问卷管理</h1>
    <p>管理固定问卷库、发布状态、完成统计和原始数据导出，保留现有问卷 API 契约。</p>
  </header>
  <div class="final-workspace ui-workspace">

  <div class="card final-toolbar-card">
    <div class="final-tabs" role="tablist" aria-label="问卷管理视图">
      <button class="btn small qm-tab" role="tab" aria-selected="true" data-tab="fixed" onclick="switchQmTab('fixed')">固定问卷库</button>
      <button class="btn small secondary qm-tab" role="tab" aria-selected="false" data-tab="publish" onclick="switchQmTab('publish')">发布管理</button>
      <button class="btn small secondary qm-tab" role="tab" aria-selected="false" data-tab="completion" onclick="switchQmTab('completion')">完成情况</button>
      <button class="btn small secondary qm-tab" role="tab" aria-selected="false" data-tab="export" onclick="switchQmTab('export')">数据导出</button>
    </div>
  </div>

  <div id="qmTabFixed" class="qm-tab-content" role="tabpanel">
    <section class="card final-section-card">
      <div class="card-hd"><h2>固定问卷库</h2></div>
      <div class="card-bd" id="fixedQList"><div class="ui-loading-state">加载中...</div></div>
    </section>
  </div>

  <div id="qmTabPublish" class="qm-tab-content" role="tabpanel" style="display:none">
    <section class="card final-section-card" style="margin-bottom:12px">
      <div class="card-hd"><h2>发布问卷</h2></div>
      <div class="card-bd qm-form-grid">
        <select id="pubQSelect"><option value="">-- 选择固定问卷 --</option></select>
        <select id="pubSessionSelect"><option value="">-- 选择课次 --</option></select>
        <select id="pubStageSelect"><option value="pre">前测</option><option value="post">后测</option></select>
        <button class="btn small" onclick="createPublication()">发布</button>
      </div>
      <div id="pubStatus" class="muted final-status-message" style="font-size:12px;margin:0 20px 20px"></div>
    </section>
    <section class="card final-section-card">
      <div class="card-hd"><h2>已发布列表</h2></div>
      <div class="card-bd" id="pubList"><div class="ui-loading-state">加载中...</div></div>
    </section>
  </div>

  <div id="qmTabCompletion" class="qm-tab-content" role="tabpanel" style="display:none">
    <section class="card final-section-card">
      <div class="card-hd"><h2>完成统计</h2></div>
      <div class="card-bd" id="compList"><div class="ui-loading-state">加载中...</div></div>
    </section>
  </div>

  <div id="qmTabExport" class="qm-tab-content" role="tabpanel" style="display:none">
    <section class="card final-section-card">
      <div class="card-hd"><h2>问卷原始数据导出</h2></div>
      <div class="card-bd">
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <select id="exportSessionSelect" style="width:auto;min-width:220px">
            <option value="">-- 全部课次 --</option>
          </select>
          <button class="btn small" onclick="doExportQuestionnaireRaw()">下载 ZIP</button>
          <span id="exportStatus" class="muted" style="font-size:12px"></span>
        </div>
        <div class="evidence" style="margin-top:12px;font-size:12px">
          导出包含已正式提交的问卷原始答案，按课次/小组/个人/问卷组织为 ZIP 文件。
          每份 CSV 仅包含原始值，不包含任何评分。
        </div>
      </div>
    </section>
  </div>
  </div>
</main>

<style>
.qm-tab-content { min-height: 200px; }
.qm-tab { transition: all 0.15s; }
.qm-detail-modal { position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.4);z-index:999;display:flex;align-items:center;justify-content:center;padding:20px }
.qm-detail-modal > div { background:var(--surface);border-radius:var(--radius-lg);padding:24px;max-width:720px;width:100%;max-height:80vh;overflow-y:auto;box-shadow:var(--shadow-lg) }
</style>

<script type="application/json" id="legacy-questionnaire-management-script-disabled">
let qmData = {};

async function loadFixedQList() {
  try {
    const data = await window.fetchJSON("/api/teacher/questionnaires/fixed");
    qmData.fixed = data.questionnaires || [];
  } catch(e) { qmData.fixed = []; }
  const list = qmData.fixed;
  const html = list.length ? list.map(function(q) {
    const timingText = q.timing==="both" ? "前后测" : (q.timing==="pre" ? "前测" : "后测");
    var statusText = q.active ? "启用" : "停用";
    return "<div class="group-card">" +
      "<div class="group-card-head"><div><div class="group-title">" + window.escapeHtml(q.title||"") + "</div>" +
      "<div class="muted" style="font-size:12px">" + window.escapeHtml(q.code||"") + " | " + timingText + " | 1-" + (q.scale_max||5) + " | " + (q.section_count||0) + "章节 | " + (q.item_count||0) + "题" +
      "</div></div><span class="badge">" + statusText + "</span></div>" +
      "<div class="evidence">" + window.escapeHtml(q.description||"") + "</div>" +
      "<div style="font-size:12px;color:var(--text-secondary)">已发布课次: " + (q.active_publication_count||0) + "</div>" +
      "<div class="teacher-actions">" +
      "<button class="btn small secondary" onclick="viewFixedDetail(" + q.id + ")">查看详情</button>" +
      "</div></div>";
  }).join("") : "<div class="evidence">暂无固定问卷。</div>";
  document.getElementById("fixedQList").innerHTML = html;
}

function viewFixedDetail(qid) {
  var q = null;
  for (var i = 0; i < qmData.fixed.length; i++) {
    if (qmData.fixed[i].id === qid) { q = qmData.fixed[i]; break; }
  }
  if (!q) return;
  var items = q.items || [];
  var itemHtml = items.length ? items.map(function(it, idx) {
    var reverseText = it.reverse_scored ? " <span style="color:var(--danger-text)">反向题</span>" : "";
    return "<div style="padding:8px 0;border-bottom:1px solid var(--border-light)">" +
      "<div style="font-weight:600;font-size:13px">Q" + (idx+1) + ". " + window.escapeHtml(it.prompt_text||"") + "</div>" +
      "<div class="muted" style="font-size:11px">维度: " + window.escapeHtml(it.dimension_label||"-") + " | 编码: " + window.escapeHtml(it.item_code||"") + reverseText +
      "</div></div>";
  }).join("") : "<div class="evidence">暂无题目。</div>";

  var modalHtml = "<div class="qm-detail-modal" onclick="if(event.target===this)this.remove()"><div>" +
    "<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">" +
    "<h2 style="margin:0">" + window.escapeHtml(q.title||"") + "</h2>" +
    "<button class="btn small secondary" onclick="this.closest('.qm-detail-modal').remove()">关闭</button></div>" +
    "<div class="muted" style="font-size:12px;margin-bottom:12px">编码: " + window.escapeHtml(q.code||"") + " | timing: " + q.timing + " | 量表: 1-" + (q.scale_max||5) + "</div>" +
    "<div style="margin-bottom:12px">" + window.escapeHtml(q.description||"") + "</div>" +
    "<h3 style="margin:12px 0 8px">题目列表</h3>" + itemHtml + "</div></div>";
  var div = document.createElement("div");
  div.innerHTML = modalHtml;
  document.body.appendChild(div.firstElementChild);
}

async function loadPubData() {
  try {
    var data = await window.fetchJSON("/api/teacher/questionnaires/fixed");
    qmData.fixed = data.questionnaires || [];
    var sel = document.getElementById("pubQSelect");
    sel.innerHTML = "<option value="">-- 选择固定问卷 --</option>" + qmData.fixed.map(function(q) {
      return "<option value="" + q.id + "">" + window.escapeHtml(q.title||q.code||"") + "</option>";
    }).join("");
  } catch(e) {}
  try {
    var data2 = await window.fetchJSON("/api/teacher/sessions");
    qmData.sessions = data2.sessions || [];
    var sel2 = document.getElementById("pubSessionSelect");
    sel2.innerHTML = "<option value="">-- 选择课次 --</option>" + qmData.sessions.map(function(s) {
      return "<option value="" + s.id + "" data-no="" + (s.session_no||0) + "">课次 #" + (s.session_no||s.id) + ": " + window.escapeHtml(s.title||"") + "</option>";
    }).join("");
  } catch(e) {}
  loadPubList();
}

async function loadPubList() {
  try {
    var data = await window.fetchJSON("/api/teacher/questionnaire-publications");
    qmData.publications = data.publications || [];
  } catch(e) { qmData.publications = []; }
  var list = qmData.publications;
  var html = list.length ? list.map(function(p) {
    var stageText = p.response_stage === "pre" ? "前测" : "后测";
    var statusText = p.status === "enabled" ? "已启用" : "已关闭";
    var toggleBtn = p.status === "enabled"
      ? "<button class="btn small secondary" onclick="togglePub(" + p.id + ",'closed')">关闭</button>"
      : "<button class="btn small" onclick="togglePub(" + p.id + ",'enabled')">启用</button>";
    return "<div class="group-card"><div class="group-card-head"><div><div class="group-title">" + window.escapeHtml(p.questionnaire_title||"") + "</div>" +
      "<div class="muted" style="font-size:12px">课次#" + (p.session_no||p.es_session_no||"") + " | " + stageText + " | " + statusText + "</div></div>" +
      "<div style="display:flex;gap:6px">" + toggleBtn +
      "<button class="btn small secondary" onclick="deletePub(" + p.id + ")">删除</button></div></div></div>";
  }).join("") : "<div class="evidence">暂无发布记录。</div>";
  document.getElementById("pubList").innerHTML = html;
}

async function createPublication() {
  var qid = document.getElementById("pubQSelect").value;
  var sid = document.getElementById("pubSessionSelect").value;
  var stage = document.getElementById("pubStageSelect").value;
  var selEl = document.getElementById("pubSessionSelect");
  var sno = parseInt(selEl.options[selEl.selectedIndex]?.getAttribute("data-no")||"0");
  if (!qid || !sid) {
    document.getElementById("pubStatus").textContent = "请选择问卷和课次";
    return;
  }
  try {
    await window.fetchJSON("/api/teacher/questionnaire-publications", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({questionnaire_id:parseInt(qid), session_id:parseInt(sid), session_no:sno, response_stage:stage})
    });
    document.getElementById("pubStatus").textContent = "发布成功！";
    loadPubList();
  } catch(e) {
    document.getElementById("pubStatus").textContent = "发布失败: "+(e.message||e);
  }
}

async function togglePub(pid, status) {
  try {
    await window.fetchJSON("/api/teacher/questionnaire-publications/"+pid, {
      method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({status:status})
    });
    loadPubList();
  } catch(e) { alert("操作失败: "+(e.message||e)); }
}

async function deletePub(pid) {
  if (!confirm("确认删除此发布？如已有学生提交，将自动转为关闭状态。")) return;
  try {
    await window.fetchJSON("/api/teacher/questionnaire-publications/"+pid, {method:"DELETE"});
    loadPubList();
  } catch(e) { alert("操作失败: "+(e.message||e)); }
}

async function loadCompletion() {
  try {
    var data = await window.fetchJSON("/api/teacher/questionnaire-completion");
    qmData.stats = data.stats || [];
  } catch(e) { qmData.stats = []; }
  var list = qmData.stats;
  var html = list.length ? "<table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr style="background:var(--border-light)">" +
    "<th style="padding:8px 10px;text-align:left;border-bottom:1px solid var(--border)">课次</th>" +
    "<th style="padding:8px 10px;text-align:left;border-bottom:1px solid var(--border)">问卷</th>" +
    "<th style="padding:8px 10px;text-align:left;border-bottom:1px solid var(--border)">阶段</th>" +
    "<th style="padding:8px 10px;text-align:left;border-bottom:1px solid var(--border)">小组</th>" +
    "<th style="padding:8px 10px;text-align:center;border-bottom:1px solid var(--border)">已完成</th>" +
    "<th style="padding:8px 10px;text-align:center;border-bottom:1px solid var(--border)">应完成</th>" +
    "<th style="padding:8px 10px;text-align:center;border-bottom:1px solid var(--border)">未完成</th></tr></thead><tbody>" +
    list.map(function(r) {
      return "<tr><td style="padding:6px 10px;border-bottom:1px solid var(--border-light)">课次#" + (r.session_no||"") + "</td>" +
      "<td style="padding:6px 10px;border-bottom:1px solid var(--border-light)">" + window.escapeHtml(r.questionnaire_title||"") + "</td>" +
      "<td style="padding:6px 10px;border-bottom:1px solid var(--border-light)">" + (r.response_stage==="pre" ? "前测" : "后测") + "</td>" +
      "<td style="padding:6px 10px;border-bottom:1px solid var(--border-light)">" + window.escapeHtml(r.group_name||r.group_code||"") + "</td>" +
      "<td style="padding:6px 10px;text-align:center;border-bottom:1px solid var(--border-light)">" + r.completed_count + "</td>" +
      "<td style="padding:6px 10px;text-align:center;border-bottom:1px solid var(--border-light)">" + r.roster_count + "</td>" +
      "<td style="padding:6px 10px;text-align:center;border-bottom:1px solid var(--border-light)">" + r.uncompleted_count + "</td></tr>";
    }).join("") +
    "</tbody></table>" : "<div class="evidence">暂无完成数据。</div>";
  document.getElementById("compList").innerHTML = html;
}

function switchQmTab(name) {
  document.querySelectorAll(".qm-tab-content").forEach(function(el) { el.style.display = "none"; });
  document.querySelectorAll(".qm-tab").forEach(function(el) {
    el.classList.remove("primary"); el.classList.add("secondary"); el.style.background = ""; el.style.color = "";
  });
  var showEl = document.getElementById("qmTab" + name.charAt(0).toUpperCase() + name.slice(1));
  if (showEl) showEl.style.display = "";
  var tabBtn = document.querySelector('.qm-tab[data-tab="' + name + '"]');
  if (tabBtn) { tabBtn.classList.remove("secondary"); tabBtn.style.background = "var(--primary)"; tabBtn.style.color = "#fff"; }
  if (name === "fixed") loadFixedQList();
  if (name === "publish") loadPubData();
  if (name === "completion") loadCompletion();
  if (name === "export") loadExportSessions();
}



async function loadExportSessions() {
  try {
    var data2 = await window.fetchJSON("/api/teacher/sessions");
    qmData.sessions = data2.sessions || [];
    var sel = document.getElementById("exportSessionSelect");
    if (sel) {
      sel.innerHTML = "<option value=\"\">-- 全部课次 --</option>" +
        qmData.sessions.map(function(s) {
          return "<option value=\"" + s.id + "\">课次 #" + (s.session_no||s.id) + ": " + window.escapeHtml(s.title||"") + "</option>";
        }).join("");
    }
  } catch(e) {}
}

async function doExportQuestionnaireRaw() {
  var sel = document.getElementById("exportSessionSelect");
  var statusEl = document.getElementById("exportStatus");
  if (!statusEl) return;
  var sessionIds = sel ? sel.value : "";
  statusEl.textContent = "正在生成导出...";
  try {
    var params = new URLSearchParams();
    if (sessionIds) { params.set("session_ids", sessionIds); }
    var url = "/api/teacher/questionnaire-raw-export?" + params.toString();
    var resp = await fetch(url, {headers: window.getDefaultHeaders ? window.getDefaultHeaders() : {}});
    if (!resp.ok) {
      var errData = await resp.json().catch(function(){return null;});
      throw new Error((errData && errData.error) || ("HTTP " + resp.status));
    }
    var blob = await resp.blob();
    var disposition = resp.headers.get("Content-Disposition") || "";
    var match = disposition.match(/filename=(.+)/);
    var filename = match ? match[1].trim() : "questionnaire_raw_export.zip";
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(function(){URL.revokeObjectURL(link.href);}, 5000);
    statusEl.textContent = "导出成功！";
  } catch(e) {
    statusEl.textContent = "导出失败: " + (e.message || e);
    statusEl.style.color = "var(--danger-text)";
    setTimeout(function(){statusEl.style.color = "";}, 5000);
  }
}

document.addEventListener("DOMContentLoaded", function() { loadFixedQList(); });
</script>
"""

    script = """
<script src="/static/teacher/teacher-api.js"></script>
<script src="/static/teacher/questionnaire-management.js"></script>
"""

    return render_template_string(teacher_shell("问卷管理 - SSRL-ESP", body, script, TEACHER_FINAL_HEAD))


