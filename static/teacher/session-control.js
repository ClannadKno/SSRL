// SSRL-ESP Session Control (T1) + Task Management

// Safe textContent setter with null check
function setTextById(id, text) {
  const el = document.getElementById(id);
  if (el) {
    el.textContent = text;
  } else {
    console.warn('[session-control] Missing #' + id + ':', text);
  }
}

// ============================================================
// T0 inline: Current Status Bar
// ============================================================
async function loadCurrentStatus() {
  const el = document.getElementById('currentStatusBar');
  if (!el) return;
  try {
    const data = await window.fetchJSON('/api/teacher/session/status');
    renderCurrentStatus(data);
  } catch (e) {
    el.innerHTML = '<div class="evidence" style="color:var(--danger-text)">状态加载失败: ' + window.escapeHtml(e.message) + '</div>';
  }
}

function renderCurrentStatus(data) {
  const el = document.getElementById('currentStatusBar');
  if (!el) return;
  const session = data.current_session;
  const roleInfo = data.session_role || '--';
  const statusLabel = session ? session.status : '无活跃课次';
  const taskTitle = data.task ? data.task.title : '未分配任务';
  const agentModeLabels = { none: '不启用智能体', strategy: '策略智能体', emotion: '情绪智能体' };
  const agentMode = data.agent_mode || (session && session.agent_mode) || 'none';
  const agentModeSummary = agentModeLabels[agentMode] || '配置无效';
  const detectionBadge = data.detection_enabled
    ? '<span class="badge" style="background:var(--success-soft);color:var(--success-text);border-color:var(--success-border)">检测开启</span>'
    : '<span class="badge" style="background:var(--border-light);color:var(--text-muted)">检测关闭</span>';
  const interventionBadge = data.intervention_enabled
    ? '<span class="badge" style="background:var(--success-soft);color:var(--success-text);border-color:var(--success-border)">干预开启</span>'
    : '<span class="badge" style="background:var(--border-light);color:var(--text-muted)">干预关闭</span>';
  const freezeBadge = data.condition_frozen
    ? '<span class="badge" style="background:var(--warning-soft);color:var(--warning-text);border-color:var(--warning-border)">已冻结</span>'
    : '<span class="badge" style="background:var(--border-light);color:var(--text-muted)">未冻结</span>';
  const durationStr = data.started_at
    ? window.formatDuration(data.elapsed_seconds) + ' (' + window.formatDt(data.started_at) + ')'
    : '--';
  const groupDiscussions = data.group_discussions || [];
  const groupCounts = groupDiscussions.reduce(function(acc, item) {
    const status = item.group_discussion_status || item.status || 'not_started';
    acc[status] = (acc[status] || 0) + 1;
    return acc;
  }, {});
  const groupTimerSummary = groupDiscussions.length
    ? [
        groupCounts.waiting ? ('等待 ' + groupCounts.waiting) : '',
        groupCounts.running ? ('讨论中 ' + groupCounts.running) : '',
        groupCounts.timed_out ? ('超时 ' + groupCounts.timed_out) : '',
        groupCounts.submitted ? ('已提交 ' + groupCounts.submitted) : ''
      ].filter(Boolean).join(' / ')
    : '尚无小组进入讨论';
  el.innerHTML = '<div class="t0-grid">' +
    '<div class="t0-item"><span class="t0-label">课次角色</span><strong>' + window.escapeHtml(roleInfo) + '</strong></div>' +
    '<div class="t0-item"><span class="t0-label">状态</span><strong>' + window.escapeHtml(statusLabel) + '</strong></div>' +
    '<div class="t0-item"><span class="t0-label">当前任务</span><strong title="' + window.escapeHtml(taskTitle) + '">' + window.escapeHtml(taskTitle) + '</strong></div>' +
    '<div class="t0-item"><span class="t0-label">检测</span>' + detectionBadge + '</div>' +
    '<div class="t0-item"><span class="t0-label">干预</span>' + interventionBadge + '</div>' +
    '<div class="t0-item"><span class="t0-label">智能体模式</span><strong>' + window.escapeHtml(agentModeSummary) + '</strong></div>' +
    '<div class="t0-item"><span class="t0-label">课次开放</span><strong>' + durationStr + '</strong></div>' +
    '<div class="t0-item"><span class="t0-label">分组讨论</span><strong>' + window.escapeHtml(groupTimerSummary) + '</strong></div>' +
    '<div class="t0-item"><span class="t0-label">Condition</span>' + freezeBadge + '</div>' +
    '<div class="t0-item"><span class="t0-label">质量警告</span><strong>' + (data.data_quality_warning_count || 0) + '</strong></div>' +
    '<div class="t0-item"><span class="t0-label">安全事件</span><strong>' + (data.safety_event_count || 0) + '</strong></div>' +
    '</div>' +
    _renderGroupDiscussionRuntimeSummary(groupDiscussions, { showMembers: true });
}

// ============================================================
// T1: Experiment Session List (expandable cards)
// ============================================================

async function loadSessionList() {
  const el = document.getElementById('sessionList');
  if (!el) return;
  try {
    const data = await window.fetchJSON('/api/teacher/sessions');
    renderSessionList(data.sessions || []);
  } catch (e) {
    el.innerHTML = '<div class="evidence" style="color:var(--danger-text)">加载失败: ' + window.escapeHtml(e.message) + '</div>';
  }
}

// Store session data for expanded details
let _sessionData = [];
let _questionnaireSets = [];

function _questionnaireSetName(setId) {
  if (!setId) return '未绑定';
  const normalizedId = parseInt(setId, 10);
  const found = _questionnaireSets.find(function(qset) { return qset.id === normalizedId; });
  return found ? (found.name || ('问卷包 #' + normalizedId)) : ('问卷包 #' + normalizedId);
}

function _renderQuestionnairePublicationSummary(s) {
  const summary = s.questionnaire_summary || {};
  function rowsFor(stage) {
    const list = summary[stage] || [];
    if (!list.length) {
      return '<div class="muted" style="font-size:12px">\u6682\u65e0\u5df2\u53d1\u5e03\u95ee\u5377</div>';
    }
    return list.map(function(pub) {
      const title = pub.questionnaire_title || ('\u95ee\u5377 #' + pub.questionnaire_id);
      const status = pub.status || '--';
      return '<div style="display:flex;justify-content:space-between;gap:8px;align-items:center;font-size:12px">' +
        '<span title="' + window.escapeHtml(pub.questionnaire_code || '') + '">' + window.escapeHtml(title) + '</span>' +
        '<span class="badge">' + window.escapeHtml(status) + '</span>' +
        '</div>';
    }).join('');
  }
  return '<div class="evidence" style="margin-top:8px">' +
    '<div style="font-weight:700;font-size:12px;margin-bottom:6px">\u5df2\u7ed1\u5b9a\u95ee\u5377</div>' +
    '<div style="display:grid;gap:8px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr))">' +
      '<div><span class="t0-label">\u524d\u6d4b\u95ee\u5377</span>' + rowsFor('pre') + '</div>' +
      '<div><span class="t0-label">\u540e\u6d4b\u95ee\u5377</span>' + rowsFor('post') + '</div>' +
    '</div></div>';
}

function _groupDiscussionStatusLabel(status) {
  if (status === 'waiting') return '等待成员';
  if (status === 'running') return '讨论中';
  if (status === 'timed_out') return '已超时';
  if (status === 'submitted') return '已提交';
  if (status === 'closed') return '已关闭';
  return '未进入';
}

function _formatGroupRemaining(seconds, status) {
  if (status === 'waiting') return '等待全组就绪';
  if (status === 'submitted') return '已提交';
  if (status === 'timed_out' || status === 'closed') return _groupDiscussionStatusLabel(status);
  if (seconds === null || seconds === undefined) return '未开始计时';
  if (Number(seconds) <= 0) return '已超时';
  const minutes = Math.max(1, Math.ceil(Number(seconds) / 60));
  return '剩余约 ' + minutes + ' 分钟';
}

function _renderGroupDiscussionMembers(group) {
  const members = Array.isArray(group.ready_students) ? group.ready_students : [];
  const memberList = members.length
    ? '<ul style="margin:4px 0 0 16px;padding:0;display:grid;gap:2px">' +
      members.map(function(member) {
        const name = typeof member === 'string'
          ? member
          : ((member && (member.name || member.real_name || member.username)) ||
            ('学生 #' + ((member && member.student_id) || '?')));
        return '<li>' + window.escapeHtml(String(name)) + '</li>';
      }).join('') +
      '</ul>'
    : '<div class="muted" style="margin-top:4px">暂无成员进入</div>';
  return '<details class="group-discussion-members" style="font-size:11px">' +
    '<summary style="cursor:pointer;color:var(--accent);white-space:nowrap">查看已进入成员</summary>' +
    '<div style="margin-top:4px;padding:5px 8px;border:1px solid var(--border-light);border-radius:6px;background:var(--surface-muted)">' +
      memberList +
    '</div>' +
    '</details>';
}

function _renderGroupDiscussionRuntimeSummary(groupDiscussions, options) {
  const items = groupDiscussions || [];
  const showMembers = Boolean(options && options.showMembers);
  if (!items.length) {
    return '<div class="evidence" style="margin-top:8px;font-size:12px">' +
      '<strong>分组讨论计时:</strong> 尚无小组进入讨论阶段</div>';
  }
  return '<div class="evidence group-discussion-runtime-summary" style="margin-top:8px">' +
    '<div style="font-weight:700;font-size:12px;margin-bottom:6px">分组讨论计时</div>' +
    '<div style="display:grid;gap:6px">' +
      items.map(function(g) {
        const name = g.group_name || g.group_code || ('小组 #' + g.group_id);
        const ready = (g.ready_student_count || 0) + '/' + (g.expected_student_count || 0);
        const status = g.group_discussion_status || g.status || '';
        const remaining = _formatGroupRemaining(g.group_remaining_seconds, status);
        const row = '<div style="display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:8px;align-items:center;font-size:12px">' +
          '<span title="' + window.escapeHtml(g.group_code || '') + '">' + window.escapeHtml(name) + '</span>' +
          '<span class="badge">' + window.escapeHtml(_groupDiscussionStatusLabel(status)) + '</span>' +
          '<span class="muted">' + window.escapeHtml(remaining) + ' · ' + window.escapeHtml(ready) + '</span>' +
        '</div>';
        return showMembers
          ? '<div style="display:grid;gap:4px">' + row + _renderGroupDiscussionMembers(g) + '</div>'
          : row;
      }).join('') +
    '</div></div>';
}

function renderSessionList(sessions) {
  const el = document.getElementById('sessionList');
  if (!el) return;
  _sessionData = sessions;
  if (!sessions.length) {
    el.innerHTML = '<div class="evidence">暂无课次，请先创建。</div>';
    return;
  }
  el.innerHTML = sessions.map(s => {
    const statusBadge = '<span class="badge' + (s.status === 'running' ? ' style="background:var(--success-soft);color:var(--success-text);border-color:var(--success-border)"' : '') + '">' + (s.status || 'draft') + '</span>';
    const taskName = s.task_title ? window.escapeHtml(s.task_title) : (s.task_id ? '任务 #' + s.task_id : '未分配');
    const questionnaireSetName = _questionnaireSetName(s.questionnaire_set_id);
    const expandedContent = _buildExpandedContent(s);
    return '<div class="group-card session-card">' +
      '<div class="group-card-head" style="cursor:pointer;user-select:none" onclick="toggleSessionExpand(' + s.id + ')">' +
      '<div><div class="group-title">课次 #' + s.id + ' - 第' + (s.session_no || '?') + ' 课时</div>' +
      '<div class="muted" style="font-size:12px;margin-top:4px">' + window.escapeHtml(s.session_role || '--') + ' | 任务: ' + taskName + ' | 问卷包: ' + window.escapeHtml(questionnaireSetName) + ' | 创建: ' + window.formatDt(s.created_at) + '</div></div>' +
      '<div style="display:flex;align-items:center;gap:8px">' + statusBadge +
      '<span id="expandIcon-' + s.id + '" style="font-size:14px;color:var(--text-muted);transition:transform 0.2s">&#9660;</span></div>' +
      '</div>' +
      '<div id="sessionDetail-' + s.id + '" class="session-detail" style="display:none;padding-top:8px;border-top:1px solid var(--border-light)">' +
      expandedContent +
      '</div>' +
      '</div>';
  }).join('');
}

function _buildExpandedContent(s) {
  const agentModeLabels = { none: '不启用智能体', strategy: '策略智能体', emotion: '情绪智能体' };
  let agentModeText = s.agent_configuration_error
    ? '配置无效，请重新选择'
    : (agentModeLabels[s.agent_mode] || '未配置');
  if (!s.agent_configuration_error && s.agent_mode === 'emotion') {
    agentModeText = '情绪智能体<br><span class="muted">后台状态监测：启用</span>';
  } else if (!s.agent_configuration_error && s.agent_mode === 'none' && s.research_state_monitoring_enabled) {
    agentModeText = '不启用智能体<br><span class="muted">后台状态监测：启用（研究）</span>';
  }
  const taskSection = s.task_id ? (
    '<div class="evidence" style="font-size:12px">' +
      '<strong>分配任务:</strong> ' + window.escapeHtml(s.task_title || ('任务 #' + s.task_id)) +
      (s.task_question ? '<br><strong>核心问题:</strong> ' + window.escapeHtml(s.task_question) : '') +
      (s.task_time_limit ? '<br><strong>时限:</strong> ' + s.task_time_limit + ' 分钟' : '') +
    '</div>'
  ) : '<div class="muted" style="font-size:12px">未分配任务</div>';

  const metaSection = '<div style="display:grid;gap:6px;font-size:12px;grid-template-columns:1fr 1fr;margin-top:8px">' +
'<div><span class="t0-label">课时</span><div>' + (s.session_no || '--') + '</div></div>' +
    '<div><span class="t0-label">角色</span><div>' + window.escapeHtml(s.session_role || '--') + '</div></div>' +
    '<div><span class="t0-label">状态</span><div>' + (s.status || '--') + '</div></div>' +
    '<div><span class="t0-label">问卷包</span><div>' + window.escapeHtml(_questionnaireSetName(s.questionnaire_set_id)) + '</div></div>' +
    '<div><span class="t0-label">创建时间</span><div>' + window.formatDt(s.created_at) + '</div></div>' +
    (s.start_time ? '<div><span class="t0-label">开始时间</span><div>' + window.formatDt(s.start_time) + '</div></div>' : '') +
    (s.end_time ? '<div><span class="t0-label">结束时间</span><div>' + window.formatDt(s.end_time) + '</div></div>' : '') +
     '<div><span class="t0-label">检测</span><div>' + (s.agent_detection_enabled ? '开启' : '关闭') + '</div></div>' +
     '<div><span class="t0-label">智能体模式</span><div>' + agentModeText + '</div></div>' +
    (s.condition_frozen !== undefined ? '<div><span class="t0-label">条件冻结</span><div>' + (s.condition_frozen ? '已冻结' : '未冻结') + '</div></div>' : '') +
    (s.archived_at ? '<div><span class="t0-label">归档时间</span><div>' + window.formatDt(s.archived_at) + '</div></div>' : '') +
    '</div>';

  // Actions
  const actions = [];
  if (s.status === 'draft') {
    actions.push('<button class="btn small" onclick="event.stopPropagation();startSession(' + s.id + ')">开始</button>');
    actions.push('<button class="btn small secondary" onclick="event.stopPropagation();editSession(' + s.id + ')">编辑</button>');
  } else if (s.status === 'running') {
    actions.push('<button class="btn small secondary" onclick="event.stopPropagation();endSession(' + s.id + ')">结束</button>');
  } else if (s.status === 'ended') {
    actions.push('<button class="btn small secondary" onclick="event.stopPropagation();archiveSession(' + s.id + ')">归档</button>');
  }
  if (s.status === 'draft') {
    actions.push('<button class="btn small secondary" onclick="event.stopPropagation();deleteSession(' + s.id + ')" style="color:var(--danger-text)">删除</button>');
  }

  // Task assignment controls
  const assignSection = s.status === 'draft' ? (
    '<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border-light)">' +
      '<div style="display:flex;gap:8px;align-items:center;font-size:12px">' +
        '<select id="assignTask-' + s.id + '" style="width:auto;min-width:140px;height:30px;font-size:12px" onclick="event.stopPropagation()">' +
          '<option value="">-- 切换任务 --</option>' +
        '</select>' +
        '<button class="btn small secondary" onclick="event.stopPropagation();assignTaskToSession(' + s.id + ')" style="height:30px;font-size:11px">分配</button>' +
        (s.task_id ? '<span class="badge" style="font-size:10px">当前: ' + window.escapeHtml(s.task_title || ('#' + s.task_id)) + '</span>' : '') +
      '</div>' +
    '</div>'
  ) : '';

  const agentRecordsSection = '<div class="evidence" style="margin-top:10px">' +
    '<div style="display:flex;justify-content:space-between;gap:8px;align-items:center">' +
      '<div><strong>情绪反馈与后台状态记录</strong>' +
      '<div class="muted" style="font-size:11px">两类数据独立展示，不互相作为实时模型输入。</div></div>' +
      '<button class="btn small secondary" onclick="event.stopPropagation();loadSessionAgentRecords(' + s.id + ')">查询记录</button>' +
    '</div>' +
    '<div id="agentRecords-' + s.id + '" class="muted" style="font-size:12px;margin-top:8px">点击“查询记录”加载。</div>' +
    '</div>';

  return taskSection + metaSection +
    _renderGroupDiscussionRuntimeSummary(s.group_discussions || []) +
    _renderQuestionnairePublicationSummary(s) +
    agentRecordsSection +
    (actions.length ? '<div class="teacher-actions" style="margin-top:10px">' + actions.join('') + '</div>' : '') +
    assignSection;
}

function _recordValue(value) {
  if (value === null || value === undefined || value === '') return '--';
  return window.escapeHtml(String(value));
}

function _recordWindow(row) {
  const start = row.current_window_start ? window.formatDt(row.current_window_start) : '--';
  const end = row.current_window_end ? window.formatDt(row.current_window_end) : '--';
  return window.escapeHtml(start + ' — ' + end);
}

function _renderEmotionFeedbackRecords(rows) {
  if (!rows.length) return '<div class="muted" style="font-size:12px">暂无情绪反馈记录。</div>';
  return '<div class="ops-table-scroll"><table class="ops-data-table" style="font-size:11px">' +
    '<thead><tr><th>小组</th><th>slot_index</th><th>时间窗口</th><th>feedback_state</th><th>confidence</th><th>final_text</th><th>fallback_used</th><th>published_at</th></tr></thead>' +
    '<tbody>' + rows.map(function(row) {
      const finalText = row.final_text || row.failure_reason || '--';
      return '<tr>' +
        '<td>' + _recordValue(row.group_id) + '</td>' +
        '<td>' + _recordValue(row.slot_index) + '</td>' +
        '<td>' + _recordWindow(row) + '</td>' +
        '<td>' + _recordValue(row.feedback_state) + '</td>' +
        '<td>' + _recordValue(row.confidence) + '</td>' +
        '<td>' + _recordValue(finalText) + '</td>' +
        '<td>' + (row.fallback_used ? '是' : '否') + '</td>' +
        '<td>' + _recordValue(row.published_at ? window.formatDt(row.published_at) : null) + '</td>' +
      '</tr>';
    }).join('') + '</tbody></table></div>';
}

function _renderCanonicalStateRecords(rows) {
  if (!rows.length) return '<div class="muted" style="font-size:12px">暂无 canonical 状态记录。</div>';
  return '<div class="ops-table-scroll"><table class="ops-data-table" style="font-size:11px">' +
    '<thead><tr><th>小组</th><th>状态时间</th><th>canonical_sub_state_code</th><th>confidence</th></tr></thead>' +
    '<tbody>' + rows.map(function(row) {
      const stateTime = row.detected_at || row.end_at || row.start_at || row.created_at;
      return '<tr>' +
        '<td>' + _recordValue(row.group_id) + '</td>' +
        '<td>' + _recordValue(stateTime ? window.formatDt(stateTime) : null) + '</td>' +
        '<td>' + _recordValue(row.canonical_sub_state_code) + '</td>' +
        '<td>' + _recordValue(row.confidence) + '</td>' +
      '</tr>';
    }).join('') + '</tbody></table></div>';
}

async function loadSessionAgentRecords(sessionId) {
  const el = document.getElementById('agentRecords-' + sessionId);
  if (!el) return;
  el.innerHTML = '<div class="muted">加载中...</div>';
  try {
    const data = await window.fetchJSON('/api/teacher/session/' + sessionId + '/emotion-feedbacks?limit=200');
    el.innerHTML = '<div style="display:grid;gap:12px">' +
      '<section><div style="font-weight:700;margin-bottom:6px">情绪反馈记录（独立分类）</div>' +
        _renderEmotionFeedbackRecords(data.emotion_feedbacks || []) + '</section>' +
      '<section><div style="font-weight:700;margin-bottom:6px">Canonical 状态时间线（独立监测）</div>' +
        _renderCanonicalStateRecords(data.canonical_states || []) + '</section>' +
      '</div>';
  } catch (e) {
    el.innerHTML = '<div style="color:var(--danger-text)">记录加载失败: ' + window.escapeHtml(e.message) + '</div>';
  }
}

function toggleSessionExpand(id) {
  const detail = document.getElementById('sessionDetail-' + id);
  const icon = document.getElementById('expandIcon-' + id);
  if (!detail || !icon) return;
  const visible = detail.style.display !== 'none';
  detail.style.display = visible ? 'none' : 'block';
  icon.style.transform = visible ? 'rotate(0deg)' : 'rotate(180deg)';
  // Load task options into the assign dropdown if it exists
  if (!visible) {
    const sel = document.getElementById('assignTask-' + id);
    if (sel) {
      _loadTaskOptionsIntoSelect(sel, _sessionData.find(s => s.id === id));
    }
  }
}

function _loadTaskOptionsIntoSelect(sel, session) {
  if (!sel) return;
  window.fetchJSON('/api/teacher/tasks').then(data => {
    const tasks = data.tasks || [];
    const currentTaskId = session ? session.task_id : null;
    sel.innerHTML = '<option value="">-- 切换任务 --</option>' +
       tasks.map(t => '<option value="' + t.id + '"' + (t.id === currentTaskId ? ' selected' : '') + '>' +
        window.escapeHtml(t.title || '任务 #' + t.id) + '</option>').join('');
  }).catch(function() {});
}

async function assignTaskToSession(sessionId) {
  const sel = document.getElementById('assignTask-' + sessionId);
  if (!sel) return;
  const taskId = sel.value;
  if (!taskId) return;
  const statusEl = document.getElementById('createSessionStatus');
  try {
    await window.fetchJSON('/api/teacher/task/assign', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, task_id: parseInt(taskId, 10) })
    });
    if (statusEl) statusEl.textContent = '任务已分配到课次 #' + sessionId;
    await loadSessionList();
    loadCurrentTaskSummary();
  } catch (e) {
    if (statusEl) statusEl.textContent = '分配失败: ' + e.message;
  }
}

// ============================================================
// Session CRUD
// ============================================================

async function createSession() {
  const sessionNo = document.getElementById('createSessionNo');
  const taskId = document.getElementById('createTaskId');
  const questionnaireSetEl = document.getElementById('createQuestionnaireSetId');
  const titleEl = document.getElementById('createSessionTitle');
  const descEl = document.getElementById('createSessionDescription');
  const statusEl = document.getElementById('createSessionStatus');
  if (!sessionNo) return;
  if (!taskId || !taskId.value) {
    if (statusEl) statusEl.textContent = '请先创建任务，再创建课次。';
    return;
  }
  const payload = {
    session_no: parseInt(sessionNo.value || '1', 10),
    task_id: parseInt(taskId.value, 10),
    title: titleEl ? titleEl.value : '',
    description: descEl ? descEl.value : '',
  };
  if (questionnaireSetEl && questionnaireSetEl.value) {
    payload.questionnaire_set_id = parseInt(questionnaireSetEl.value, 10);
  }
  const agentModeEl = document.querySelector('input[name="createAgentMode"]:checked');
  payload.agent_mode = agentModeEl ? agentModeEl.value : 'none';
  try {
    await window.fetchJSON('/api/teacher/session/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (statusEl) statusEl.textContent = '课次已创建';
    await loadSessionList();
    loadCurrentStatus();
    loadCurrentTaskSummary();
  } catch (e) {
    if (statusEl) statusEl.textContent = '创建失败: ' + e.message;
  }
}

async function startSession(sessionId) {
  const statusEl = document.getElementById('createSessionStatus');
  try {
    await window.fetchJSON('/api/teacher/session/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId })
    });
    if (statusEl) statusEl.textContent = '课次已开始';
    await loadSessionList();
    loadCurrentStatus();
    loadCurrentTaskSummary();
  } catch (e) {
    if (statusEl) statusEl.textContent = '启动失败: ' + e.message;
  }
}

async function endSession(sessionId) {
  const statusEl = document.getElementById('createSessionStatus');
  try {
    await window.fetchJSON('/api/teacher/session/end', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId })
    });
    if (statusEl) statusEl.textContent = '课次已结束';
    await loadSessionList();
    loadCurrentStatus();
    loadCurrentTaskSummary();
  } catch (e) {
    if (statusEl) statusEl.textContent = '结束失败: ' + e.message;
  }
}

async function archiveSession(sessionId) {
  const statusEl = document.getElementById('createSessionStatus');
  try {
    await window.fetchJSON('/api/teacher/session/archive', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId })
    });
    if (statusEl) statusEl.textContent = '课次已归档';
    await loadSessionList();
    loadCurrentStatus();
    loadCurrentTaskSummary();
  } catch (e) {
    if (statusEl) statusEl.textContent = '归档失败: ' + e.message;
  }
}

// Load task options into create session select
async function loadTaskOptions() {
  const sel = document.getElementById('createTaskId');
  if (!sel) return;
  try {
    const data = await window.fetchJSON('/api/teacher/tasks');
    const tasks = data.tasks || [];
    sel.innerHTML = '<option value="">-- 选择任务 --</option>' +
      tasks.map(t => '<option value="' + t.id + '">' + window.escapeHtml(t.title || '任务 #' + t.id) + '</option>').join('');
  } catch (_) {}
}

async function loadQuestionnaireSetOptions() {
  const sel = document.getElementById('createQuestionnaireSetId');
  try {
    const data = await window.fetchJSON('/api/teacher/questionnaire-sets');
    _questionnaireSets = data.questionnaire_sets || [];
    if (sel) {
      sel.innerHTML = '<option value="">-- 不绑定问卷包 --</option>' +
        _questionnaireSets.map(function(qset) {
          return '<option value="' + qset.id + '">' + window.escapeHtml(qset.name || ('问卷包 #' + qset.id)) + '</option>';
        }).join('');
    }
  } catch (_) {
    _questionnaireSets = [];
    if (sel) sel.innerHTML = '<option value="">-- 问卷包加载失败 --</option>';
  }
}

// ============================================================
// Task Management (from task_admin.html)
// ============================================================

let selTaskId = null;
let taskPayload = null;

async function createNewPhase() {
  const name = prompt('\u8bf7\u8f93\u5165\u65b0\u5b9e\u9a8c\u9636\u6bb5\u540d\u79f0\uff1a');
  if (!name || !name.trim()) return;
  window.fetchJSON('/api/teacher/experiment-phases', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name: name.trim()})
  }).then(data => {
    if (data.ok) {
      loadExperimentPhases();
      setTextById('taskAdminStatus', '\u9636\u6bb5\u5df2\u521b\u5efa');
    }
  }).catch(e => {
    setTextById('taskAdminStatus', '\u521b\u5efa\u5931\u8d25: ' + e.message);
  });
}

function loadExperimentPhases() {
  const sel = document.getElementById('taskExperimentPhaseId');
  if (!sel) return;
  window.fetchJSON('/api/teacher/experiment-phases').then(data => {
    const phases = data.experiment_phases || [];
    sel.innerHTML = '<option value="">-- \u9009\u62e9\u9636\u6bb5 --</option>' +
      phases.map(p => '<option value="' + p.id + '">' + window.escapeHtml(p.name) + '</option>').join('');
  }).catch(function() {});
}

async function loadTaskAdmin() {
  try {
    const data = await window.fetchJSON('/api/teacher/tasks');
    renderTaskCards(data);
    loadExperimentPhases();
  } catch (_) {}
}


function fillTaskForm(task) {
  selTaskId = task ? task.id : null;
  setTextById('taskFormTitle', task ? ('\u7f16\u8f91\u4efb\u52a1 #' + task.id) : '\u65b0\u5efa\u4efb\u52a1');
  document.getElementById('taskTitle').value = task ? (task.title || '') : '';
  document.getElementById('taskTimeLimitMinutes').value = task ? (task.time_limit_minutes || 30) : 30;
  // taskActive removed - task enable/disable no longer used
  
  document.getElementById('taskExperimentPhaseId').value = task ? (task.experiment_phase_id || '') : '';
  // Clear and fill structured fields
  var structFields = ['taskBrief','taskBackground','taskSurveyNote','taskSurveyItems','taskOptions','taskBudgetTotal','taskBudgetUnit','taskBudgetMinSelected','taskConstraints','taskDiscussionQuestions','taskSubmissionRequirements','taskPreSubmitChecklist'];
  structFields.forEach(function(id) { document.getElementById(id).value = ''; });
  document.getElementById('taskBudgetUnit').value = '\u4e07\u5143';
  if (task && task.task_payload) {
    var tp = task.task_payload;
    document.getElementById('taskBrief').value = tp.task_brief || '';
    document.getElementById('taskBackground').value = (tp.background || []).join('\n');
    document.getElementById('taskSurveyNote').value = (tp.survey && tp.survey.note) || '';
    document.getElementById('taskSurveyItems').value = (tp.survey && tp.survey.items || []).map(function(item) {
      return (item.label || '') + '|' + (item.percent || 0);
    }).join('\n');
    document.getElementById('taskOptions').value = (tp.options || []).map(function(opt) {
      return [opt.name, opt.cost, opt.unit, opt.main_function, opt.concern].join('|');
    }).join('\n');
    document.getElementById('taskBudgetTotal').value = (tp.budget && tp.budget.total) || '';
    document.getElementById('taskBudgetUnit').value = (tp.budget && tp.budget.unit) || '\u4e07\u5143';
    document.getElementById('taskBudgetMinSelected').value = (tp.budget && tp.budget.min_selected) || '';
    document.getElementById('taskConstraints').value = (tp.constraints || []).join('\n');
    document.getElementById('taskDiscussionQuestions').value = (tp.discussion_questions || []).join('\n');
    document.getElementById('taskSubmissionRequirements').value = (tp.submission_requirements || []).join('\n');
    document.getElementById('taskPreSubmitChecklist').value = (tp.pre_submit_checklist || []).join('\n');
  }
}

function resetTaskForm() { fillTaskForm(null); }

function collectTaskPayload() {
  function lines(val) { return (val || '').split(/\n/).map(function(s){return s.trim();}).filter(Boolean); }
  function pipeParts(line) { return line.split('|').map(function(s){return s.trim();}); }
  var bgLines = lines(document.getElementById('taskBackground').value);
  var surveyLines = lines(document.getElementById('taskSurveyItems').value);
  var surveyItems = surveyLines.map(function(l) {
    var parts = pipeParts(l);
    return {label: parts[0] || '', percent: parseFloat(parts[1]) || 0};
  });
  var optLines = lines(document.getElementById('taskOptions').value);
  var options = optLines.map(function(l) {
    var parts = pipeParts(l);
    return {name: parts[0] || '', cost: parseFloat(parts[1]) || 0, unit: parts[2] || '\u4e07\u5143', main_function: parts[3] || '', concern: parts[4] || ''};
  });
  return {
    title: document.getElementById('taskTitle').value.trim(),
    time_limit_minutes: parseInt(document.getElementById('taskTimeLimitMinutes').value || '30', 10),
    experiment_phase_id: document.getElementById('taskExperimentPhaseId').value ? parseInt(document.getElementById('taskExperimentPhaseId').value, 10) : null,
    task_type: 'structured_decision',
    question: document.getElementById('taskTitle').value.trim(),
    task_goal: '',
    output_requirement: '',
    task_payload: {
      task_brief: (document.getElementById('taskBrief').value || '').trim(),
      background: bgLines,
      survey: {items: surveyItems, note: (document.getElementById('taskSurveyNote').value || '').trim()},
      budget: {total: parseFloat(document.getElementById('taskBudgetTotal').value || '0'), unit: (document.getElementById('taskBudgetUnit').value || '\u4e07\u5143').trim(), min_selected: parseInt(document.getElementById('taskBudgetMinSelected').value || '0', 10)},
      options: options,
      constraints: lines(document.getElementById('taskConstraints').value),
      discussion_questions: lines(document.getElementById('taskDiscussionQuestions').value),
      submission_requirements: lines(document.getElementById('taskSubmissionRequirements').value),
      pre_submit_checklist: lines(document.getElementById('taskPreSubmitChecklist').value)
    }
  };
}

function renderTaskCards(data) {
  taskPayload = data;
  setTextById('taskListSummary', (data.tasks || []).length + ' \u4e2a\u4efb\u52a1');
  const box = document.getElementById('taskCards');
  if (!box) {
    console.warn('[session-control] Missing #taskCards');
    return;
  }
  const tasks = data.tasks || [];
  if (!tasks.length) {
    box.innerHTML = '<div class="evidence">\u6682\u65e0\u4efb\u52a1\u3002</div>';
    return;
  }
  box.innerHTML = tasks.map(function(task) {
    var bits = [];
    bits.push('<span class="badge">' + (task.time_limit_minutes || 30) + ' \u5206\u949f</span>');
    if (task.experiment_phase_name) bits.push('<span class="badge" style="background:var(--accent-dim);color:var(--accent);border-color:var(--accent-border)">' + window.escapeHtml(task.experiment_phase_name) + '</span>');

    return '<div class="group-card">' +
      '<div class="group-card-head"><div><div class="group-title">' + window.escapeHtml(task.title) + '</div>' +
      '<div class="muted" style="font-size:12px;margin-top:4px">' + bits.join(' ') + '</div></div></div>' +
      '<div class="evidence">' + window.escapeHtml(task.question || '') + '</div>' +
      '<div class="teacher-actions">' +
      '<button class="btn small secondary" onclick="fillTaskForm(taskPayload.tasks.find(function(t){return t.id===' + task.id + ';}))">\u8f7d\u5165\u7f16\u8f91</button>' +
      
      '<button class="btn small secondary" onclick="deleteTask(' + task.id + ')" style="color:var(--danger-text)">\u5220\u9664</button>' +
      '</div></div>';
  }).join('') || '<div class="evidence">\u6682\u65e0\u4efb\u52a1\u3002</div>';
}


// ============================================================
// Task CRUD (called from session_control.html)
// ============================================================

// Session CRUD (additional)
// ============================================================
async function deleteSession(sessionId) {
  if (!confirm('\u786e\u5b9a\u5220\u9664\u8be5\u8bfe\u6b21\u5417\uff1f\u4ec5\u8349\u7a3f\u8bfe\u6b21\u53ef\u5220\u9664\uff0c\u6b64\u64cd\u4f5c\u4e0d\u53ef\u6062\u590d\u3002')) return;
  try {
    await window.fetchJSON('/api/teacher/session/' + sessionId, {
      method: 'DELETE'
    });
    await loadSessionList();
    loadCurrentStatus();
    loadCurrentTaskSummary();
  } catch (e) {
    alert('\u5220\u9664\u5931\u8d25\uff1a' + (e.message || '\u4ec5\u8349\u7a3f\u8bfe\u6b21\u53ef\u5220\u9664\u6216\u8be5\u8bfe\u6b21\u5df2\u88ab\u4f7f\u7528\u3002'));
    console.error(e);
  }
}
function loadCurrentTaskSummary() {
  var el = document.getElementById('currentTaskSummary');
  if (!el) return;
  window.fetchJSON('/api/teacher/session/status').then(function(data) {
    var session = data.current_session;
    var task = data.task;
    if (!session) {
      el.innerHTML = '\u6682\u65e0\u5f53\u524d\u8bfe\u6b21';
      el.className = 'muted';
    } else if (!task) {
      el.innerHTML = '\u5f53\u524d\u8bfe\u6b21\u672a\u7ed1\u5b9a\u4efb\u52a1';
      el.className = 'muted';
    } else {
      var phaseName = task.experiment_phase_name ? ' [' + task.experiment_phase_name + ']' : '';
      el.innerHTML = '<strong>' + window.escapeHtml(task.title || '\u4efb\u52a1 #' + task.id) + '</strong>' +
        phaseName + '<br><span class="muted">' +
        window.escapeHtml((task.task_payload && task.task_payload.task_brief) || task.question || '') +
        '</span>';
      el.className = 'evidence';
    }
  }).catch(function() {
    el.innerHTML = '\u65e0\u6cd5\u8bfb\u53d6\u5f53\u524d\u4efb\u52a1';
    el.className = 'muted';
  });
}
function editSession(sessionId) {
  var session = _sessionData.find(function(s) { return s.id === sessionId; });
  if (!session) return;
  var detail = document.getElementById('sessionDetail-' + sessionId);
  if (!detail) return;
  Promise.all([
    window.fetchJSON('/api/teacher/tasks'),
    _questionnaireSets.length ? Promise.resolve({questionnaire_sets: _questionnaireSets}) : window.fetchJSON('/api/teacher/questionnaire-sets')
  ]).then(function(results) {
    var tasks = results[0].tasks || [];
    _questionnaireSets = results[1].questionnaire_sets || [];
    var opts = tasks.map(function(t) {
      var sel = t.id === session.task_id ? ' selected' : '';
      return '<option value="' + t.id + '"' + sel + '>' + (t.title || '\u4efb\u52a1 #' + t.id) + '</option>';
    }).join('');
    var currentQuestionnaireSetId = session.questionnaire_set_id ? parseInt(session.questionnaire_set_id, 10) : null;
    var qsetOpts = '<option value="">-- 不绑定问卷包 --</option>' + _questionnaireSets.map(function(qset) {
      var selected = qset.id === currentQuestionnaireSetId ? ' selected' : '';
      return '<option value="' + qset.id + '"' + selected + '>' + window.escapeHtml(qset.name || ('问卷包 #' + qset.id)) + '</option>';
    }).join('');
    var editForm = [
      '<div style="padding:8px 0">',
      '<h4 style="margin:0 0 12px 0;font-size:14px">\u7f16\u8f91\u8bfe\u6b21 #' + sessionId + '</h4>',
      '<div style="display:grid;gap:8px">',
      '<div><label style="font-size:12px;font-weight:600">\u8bfe\u6b21\u540d\u79f0</label>',
      '<input id="editTitle-' + sessionId + '" type="text" value="' + (session.title || '') + '" style="width:100%;height:34px;font-size:13px"></div>',
      '<div><label style="font-size:12px;font-weight:600">\u8bfe\u6b21\u8bf4\u660e</label>',
      '<textarea id="editDescription-' + sessionId + '" style="width:100%;min-height:48px;font-size:13px">' + (session.description || '') + '</textarea></div>',
      '<div><label style="font-size:12px;font-weight:600">\u7ed1\u5b9a\u4efb\u52a1</label>',
      '<select id="editTaskId-' + sessionId + '" style="width:100%;height:34px;font-size:13px">' + opts + '</select></div>',
      '<div><label style="font-size:12px;font-weight:600">绑定问卷包</label>',
      '<select id="editQuestionnaireSetId-' + sessionId + '" style="width:100%;height:34px;font-size:13px">' + qsetOpts + '</select></div>',
      '<div style="display:flex;gap:12px;align-items:center" aria-label="智能体模式">',
      '<label style="font-size:12px;display:flex;align-items:center;gap:4px;cursor:pointer"><input name="editAgentMode-' + sessionId + '" type="radio" value="none"' + (session.agent_mode === 'none' ? ' checked' : '') + '> 不启用智能体</label>',
      '<label style="font-size:12px;display:flex;align-items:center;gap:4px;cursor:pointer"><input name="editAgentMode-' + sessionId + '" type="radio" value="strategy"' + (session.agent_mode === 'strategy' ? ' checked' : '') + '> 策略智能体</label>',
      '<label style="font-size:12px;display:flex;align-items:center;gap:4px;cursor:pointer"><input name="editAgentMode-' + sessionId + '" type="radio" value="emotion"' + (session.agent_mode === 'emotion' ? ' checked' : '') + '> 情绪智能体</label>',
      '</div>',
      '<div style="display:flex;gap:8px;margin-top:8px">',
      '<button class="btn small" onclick="saveSessionEdit(' + sessionId + ')">\u4fdd\u5b58</button>',
      '<button class="btn small secondary" onclick="cancelSessionEdit(' + sessionId + ')">\u53d6\u6d88</button>',
      '<span id="editStatus-' + sessionId + '" class="muted" style="font-size:11px"></span>',
      '</div></div></div>'
    ].join('\n');
    detail.innerHTML = editForm;
  });
}
function saveSessionEdit(sessionId) {
  var statusEl = document.getElementById('editStatus-' + sessionId);
  var payload = {};
  var titleEl = document.getElementById('editTitle-' + sessionId);
  if (titleEl) payload.title = titleEl.value;
  var descEl = document.getElementById('editDescription-' + sessionId);
  if (descEl) payload.description = descEl.value;
  var taskEl = document.getElementById('editTaskId-' + sessionId);
  if (taskEl && taskEl.value) payload.task_id = parseInt(taskEl.value, 10);
  var questionnaireSetEl = document.getElementById('editQuestionnaireSetId-' + sessionId);
  if (questionnaireSetEl) {
    payload.questionnaire_set_id = questionnaireSetEl.value ? parseInt(questionnaireSetEl.value, 10) : null;
  }
  var agentModeEl = document.querySelector('input[name="editAgentMode-' + sessionId + '"]:checked');
  if (agentModeEl) payload.agent_mode = agentModeEl.value;
  window.fetchJSON('/api/teacher/session/' + sessionId, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  }).then(function(data) {
    if (statusEl) statusEl.textContent = '\u8bfe\u6b21\u5df2\u66f4\u65b0';
    loadSessionList();
    loadCurrentStatus();
  }).catch(function(e) {
    if (statusEl) statusEl.textContent = '\u66f4\u65b0\u5931\u8d25: ' + e.message;
  });
}
function cancelSessionEdit(sessionId) {
  loadSessionList();
}

function saveCurrentSession() {
  var input = document.getElementById('currentSessionNo');
  if (!input) return;
  var sessionNo = parseInt(input.value || '1', 10);
  window.fetchJSON('/api/teacher/tasks/current-session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_no: sessionNo })
  }).then(function(data) {
    var el = document.getElementById('taskAdminStatus');
    if (el) el.textContent = '当前课时已保存';
  }).catch(function(e) {
    var el = document.getElementById('taskAdminStatus');
    if (el) el.textContent = '保存失败: ' + e.message;
  });
}

async function createTask() {
  var payload = collectTaskPayload();
  if (!payload.title) {
    setTextById('taskAdminStatus', '请输入任务标题');
    return;
  }
  try {
    var data = await window.fetchJSON('/api/teacher/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    setTextById('taskAdminStatus', '任务已创建');
    renderTaskCards(data);
    await loadExperimentPhases();
    loadTaskOptions();
    loadCurrentTaskSummary();
  } catch (e) {
    setTextById('taskAdminStatus', '创建失败: ' + e.message);
    console.error(e);
  }
}

async function saveTaskEdit() {
  if (!selTaskId) {
    setTextById('taskAdminStatus', '请先加载一个任务');
    return;
  }
  var payload = collectTaskPayload();
  if (!payload.title) {
    setTextById('taskAdminStatus', '请输入任务标题');
    return;
  }
  try {
    var data = await window.fetchJSON('/api/teacher/tasks/' + selTaskId, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    setTextById('taskAdminStatus', '任务已更新');
    selTaskId = null;
    setTextById('taskFormTitle', '新建任务');
    renderTaskCards(data);
    loadExperimentPhases();
    loadTaskOptions();
    loadCurrentTaskSummary();
  } catch (e) {
    setTextById('taskAdminStatus', '更新失败: ' + e.message);
    console.error(e);
  }
}

// setCurrentTask removed

// toggleTaskActive removed

async function deleteTask(taskId) {
  if (!confirm('确定删除该任务吗？如果任务已被课次使用，将无法删除。')) return;
  try {
    var data = await window.fetchJSON('/api/teacher/tasks/' + taskId, {
      method: 'DELETE'
    });
    setTextById('taskAdminStatus', '任务已删除');
    renderTaskCards(data);
    loadSessionList();
    loadCurrentStatus();
    loadCurrentTaskSummary();
  } catch (e) {
    setTextById('taskAdminStatus', '删除失败: ' + e.message);
    alert('删除失败: ' + (e.message || '该任务已被课次使用，请先修改或删除相关草稿课次。'));
    console.error(e);
  }
}
