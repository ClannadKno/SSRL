// SSRL-ESP Teacher Global Status Bar (T0)
// Polls /api/teacher/status/current every 30s and updates the DOM.
// Included via teacher_shell() on all teacher-facing pages.

(function() {
  'use strict';

  var GS_POLL_INTERVAL = 30000; // 30 seconds
  var gsTimer = null;

  function getOrCreateContainer() {
    var bar = document.getElementById('gsBar');
    if (bar) return bar;
    // Create container if it doesn't exist (defensive)
    bar = document.createElement('div');
    bar.id = 'gsBar';
    bar.className = 'gs-bar';
    var inner = document.createElement('div');
    inner.className = 'gs-inner';
    inner.id = 'gsContent';
    inner.innerHTML = '<span class="gs-item"><span class="gs-label">\u52a0\u8f7d\u4e2d...</span></span>';
    bar.appendChild(inner);
    document.body.insertBefore(bar, document.body.firstChild);
    return bar;
  }

  function gsFetchStatus() {
    var contentEl = document.getElementById('gsContent');
    if (!contentEl) {
      contentEl = getOrCreateContainer().querySelector('.gs-inner');
    }
    contentEl.innerHTML = '<span class="gs-item"><span class="gs-label">\u52a0\u8f7d\u4e2d...</span></span>';

    fetch('/api/teacher/status/current', { credentials: 'same-origin' })
      .then(function(r) {
        if (!r.ok) throw new Error('\u72b6\u6001\u8bf7\u6c42\u5931\u8d25: ' + r.status);
        return r.json();
      })
      .then(function(data) {
        gsRenderStatus(data);
      })
      .catch(function(err) {
        var el = document.getElementById('gsContent');
        if (el) {
          el.innerHTML = '<span class="gs-item" style="color:var(--danger-text);font-weight:600;">\u72b6\u6001\u83b7\u53d6\u5931\u8d25</span>';
        }
      });
  }

  function gsRenderStatus(data) {
    var el = document.getElementById('gsContent');
    if (!el) return;

    var session = data.current_session || null;
    var roleLabel = session ? gsEscape(session.session_role || '--') : '\u6682\u65e0';
    var statusLabel = session ? gsEscape(session.status || '--') : '\u6682\u65e0';
    var sessionNo = session ? (session.session_no || '--') : '--';
    var taskLabel = '--';
    if (data.task && data.task.title) {
      taskLabel = gsEscape(data.task.title);
    } else if (session && session.task_id) {
      taskLabel = '\u5df2\u5206\u914d\u4efb\u52a1 #' + session.task_id;
    }

    // Detection/Intervention badges
    var detBadge = data.agent_flags && data.agent_flags.detection_enabled
      ? '<span class="gs-badge gs-badge-on">\u5f00</span>'
      : '<span class="gs-badge gs-badge-off">\u5173</span>';
    var intBadge = data.agent_flags && data.agent_flags.intervention_enabled
      ? '<span class="gs-badge gs-badge-on">\u5f00</span>'
      : '<span class="gs-badge gs-badge-off">\u5173</span>';

    // Status badge
    var statusBadge = '';
    if (session) {
      if (session.status === 'running') {
        statusBadge = '<span class="gs-badge gs-badge-running">\u8fd0\u884c\u4e2d</span>';
      } else if (session.status === 'draft') {
        statusBadge = '<span class="gs-badge gs-badge-draft">\u8349\u7a3f</span>';
      } else if (session.status === 'ended') {
        statusBadge = '<span class="gs-badge gs-badge-ended">\u5df2\u7ed3\u675f</span>';
      } else if (session.status === 'archived') {
        statusBadge = '<span class="gs-badge gs-badge-archived">\u5f52\u6863</span>';
      }
    } else {
      statusBadge = '<span class="gs-badge gs-badge-off">\u6682\u65e0</span>';
    }

    // Frozen badge
    var frozenBadge = (data.condition && data.condition.frozen)
      ? '<span class="gs-badge gs-badge-frozen">\u5df2\u51bb\u7ed3</span>'
      : '<span class="gs-badge gs-badge-off">\u672a\u51bb\u7ed3</span>';

    // Duration
    var durationStr = '--';
    if (session && session.started_at) {
      var secs = session.elapsed_seconds || 0;
      durationStr = gsFormatDuration(secs);
    }

    // Data quality & safety badges
    var dqCount = (data.data_quality && data.data_quality.warning_count) || 0;
    var dqSevere = (data.data_quality && data.data_quality.severe_count) || 0;
    var safeCount = (data.safety && data.safety.event_count) || 0;

    var dqBadge = '';
    if (dqCount > 0) {
      var dqClass = dqSevere > 0 ? 'gs-badge-critical' : 'gs-badge-warn';
      dqBadge = '<span class="gs-badge ' + dqClass + '">' + dqCount + '</span>';
    } else {
      dqBadge = '<span class="gs-badge gs-badge-off">0</span>';
    }

    var safeBadge = '';
    if (safeCount > 0) {
      var safeClass = (data.safety && data.safety.unresolved_count > 0) ? 'gs-badge-critical' : 'gs-badge-warn';
      safeBadge = '<span class="gs-badge ' + safeClass + '">' + safeCount + '</span>';
    } else {
      safeBadge = '<span class="gs-badge gs-badge-off">0</span>';
    }

    // Build HTML
    el.innerHTML =
      '<span class="gs-item"><span class="gs-label">\u8bfe\u6b21</span><strong>' + gsEscape(String(sessionNo)) + '</strong></span>' +
      '<span class="gs-divider"></span>' +
      '<span class="gs-item"><span class="gs-label">\u89d2\u8272</span><strong>' + gsEscape(roleLabel) + '</strong></span>' +
      '<span class="gs-divider"></span>' +
      '<span class="gs-item"><span class="gs-label">\u72b6\u6001</span>' + statusBadge + '</span>' +
      '<span class="gs-divider"></span>' +
      '<span class="gs-item gs-title-item" title="' + gsEscape(taskLabel) + '"><span class="gs-label">\u4efb\u52a1</span><strong class="gs-truncate">' + taskLabel + '</strong></span>' +
      '<span class="gs-divider"></span>' +
      '<span class="gs-item"><span class="gs-label">\u68c0\u6d4b</span>' + detBadge + '</span>' +
      '<span class="gs-divider"></span>' +
      '<span class="gs-item"><span class="gs-label">\u5e72\u9884</span>' + intBadge + '</span>' +
      '<span class="gs-divider"></span>' +
      '<span class="gs-item"><span class="gs-label">Duration</span><strong>' + durationStr + '</strong></span>' +
      '<span class="gs-divider"></span>' +
      '<span class="gs-item"><span class="gs-label">Condition</span>' + frozenBadge + '</span>' +
      '<span class="gs-divider"></span>' +
      '<span class="gs-item"><span class="gs-label">\u8d28\u91cf\u8b66\u544a</span>' + dqBadge + '</span>' +
      '<span class="gs-divider"></span>' +
      '<span class="gs-item"><span class="gs-label">\u5b89\u5168\u4e8b\u4ef6</span>' + safeBadge + '</span>';
  }

  function gsEscape(s) {
    return (s || '').replace(/[&<>"']/g, function(m) {
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'})[m];
    });
  }

  function gsFormatDuration(seconds) {
    var value = Number(seconds || 0);
    if (!value) return '<1 \u5206\u949f';
    var mins = Math.floor(value / 60);
    var hrs = Math.floor(mins / 60);
    var remainMins = mins % 60;
    if (hrs) return hrs + 'h ' + remainMins + 'm';
    return mins + 'm';
  }

  // Initialize
  function gsInit() {
    getOrCreateContainer();
    gsFetchStatus();
    if (gsTimer) clearInterval(gsTimer);
    gsTimer = setInterval(gsFetchStatus, GS_POLL_INTERVAL);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', gsInit);
  } else {
    gsInit();
  }
})();
