// SSRL-ESP Teacher Dashboard (T0 + module entry cards)

window.loadTeacherDashboard = async function() {
  loadT0Status();
};

// ============================================================
// T0: Global Status Bar
// ============================================================
async function loadT0Status() {
  const el = document.getElementById('t0StatusBar');
  if (!el) return;
  try {
    const data = await window.fetchJSON('/api/teacher/session/status');
    renderT0Status(data);
  } catch (e) {
    el.innerHTML = '<div class="evidence" style="color:var(--danger-text)">状态加载失败: ' + window.escapeHtml(e.message) + '</div>';
  }
}

function renderT0Status(data) {
  const el = document.getElementById('t0StatusBar');
  if (!el) return;

  const session = data.current_session;
  const statusLabel = session ? session.status : '无活动课次';
  const roleInfo = data.session_role || '--';
  const taskTitle = data.task ? data.task.title : '未分配任务';
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

  el.innerHTML = '<div class="t0-grid">' +
    '<div class="t0-item"><span class="t0-label">课次角色</span><strong>' + window.escapeHtml(roleInfo) + '</strong></div>' +
    '<div class="t0-item"><span class="t0-label">状态</span><strong>' + window.escapeHtml(statusLabel) + '</strong></div>' +
    '<div class="t0-item"><span class="t0-label">任务</span><strong>' + window.escapeHtml(taskTitle) + '</strong></div>' +
    '<div class="t0-item"><span class="t0-label">检测</span>' + detectionBadge + '</div>' +
    '<div class="t0-item"><span class="t0-label">干预</span>' + interventionBadge + '</div>' +
    '<div class="t0-item"><span class="t0-label">计时</span><strong>' + durationStr + '</strong></div>' +
    '<div class="t0-item"><span class="t0-label">Condition</span>' + freezeBadge + '</div>' +
    '<div class="t0-item"><span class="t0-label">质量警告</span><strong>' + (data.data_quality_warning_count || 0) + '</strong></div>' +
    '<div class="t0-item"><span class="t0-label">安全事件</span><strong>' + (data.safety_event_count || 0) + '</strong></div>' +
    '</div>';
}

// Refresh T0 every 30s
if (document.getElementById('t0StatusBar')) {
  window.loadT0Status();
  setInterval(window.loadT0Status, 30000);
}
