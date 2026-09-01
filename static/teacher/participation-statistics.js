
// SSRL-ESP T3 Member Text Participation Statistics
// Simplified: single session+group selector, renders line chart + bar charts + detail table.
// Timeline API: /api/teacher/group/<id>/participation-timeline (every 30s)
// Summary API:  /api/teacher/group/<id>/participation-summary (on load)

(function() {
  'use strict';

  const POLL_MS = 30000;
  var pollTimer = null;
  var lastSummary = null;
  var lastTimeline = null;
  var currentMetric = 'message_count';
  var PARTICIPATION_COLORS = [
    'var(--analytics-series-1, #2f6f9f)',
    'var(--analytics-series-2, #b95858)',
    'var(--analytics-series-3, #3f816a)',
    'var(--analytics-series-4, #b8782f)',
    'var(--analytics-series-5, #6e64a5)',
    'var(--analytics-series-6, #3f8190)',
    'var(--analytics-series-7, #9b5d7e)',
    'var(--analytics-series-8, #71843c)',
    'var(--analytics-series-9, #397d78)',
    'var(--analytics-series-10, #765aa0)',
    'var(--analytics-series-11, #99643c)',
    'var(--analytics-series-12, #4d6172)'
  ];

  // --- Init ---
  if (typeof window.fetchJSON !== 'function') {
    (function() {
      var token = '';
      try {
        var urlToken = new URLSearchParams(window.location.search).get('tab_token');
        if (urlToken) sessionStorage.setItem('SSRL_ESP_TAB_TOKEN', urlToken);
        token = sessionStorage.getItem('SSRL_ESP_TAB_TOKEN') || urlToken || '';
      } catch (_) {
        try { token = new URLSearchParams(window.location.search).get('tab_token') || ''; } catch (_2) {}
      }
      window.fetchJSON = async function(url, options) {
        options = options || {};
        var headers = Object.assign({}, options.headers || {});
        if (token) headers['X-Tab-Token'] = token;
        options.headers = headers;
        var res = await fetch(url, options);
        if (!res.ok) {
          var errMsg = 'Request failed: ' + res.status;
          try { var errData = await res.json(); if (errData.error) errMsg = errData.error; } catch (_) {}
          throw new Error(errMsg);
        }
        return await res.json();
      };
    })();
  }

  window.initParticipationStats = function() {
    loadSessions();
    loadGroups();
    setupFilterAutoLoad();
    window.psLoadData = fetchParticipationData;
    window.psToggleAutoRefresh = psToggleAutoRefresh;
    window.psRenderTimeline = renderTimelineChart;
    psToggleAutoRefresh(true);
  };

  window.reloadGroupsForSession = async function() {
    await loadGroups();
  };

  // --- Session loading ---
  async function loadSessions() {
    try {
      var data = await window.fetchJSON('/api/teacher/sessions?all=true');
      var sel = document.getElementById('session-select');
      sel.innerHTML = '<option value="">-- 选择 Session --</option>';
      if (data.sessions) {
        for (var i = 0; i < data.sessions.length; i++) {
          var s = data.sessions[i];
          var opt = document.createElement('option');
          opt.value = s.id;
          var st = s.start_time ? s.start_time.slice(5,16) : "";
          opt.textContent = "\u8bfe\u6b21 #" + s.id + " | \u7b2c" + (s.session_no || "?") + " \u8bfe\u65f6" + (s.session_role ? " (" + s.session_role + ")" : "") + (st ? " " + st : "")
          sel.appendChild(opt);
        }
      }
    } catch (e) {
      console.warn('loadSessions error:', e);
    }
  }

  // --- Group loading ---
  async function loadGroups() {
    try {
      var data = await window.fetchJSON('/api/teacher/groups?all=true');
      var sel = document.getElementById('group-select');
      sel.innerHTML = '<option value="">-- 选择 Group --</option>';
      if (data.groups) {
        for (var i = 0; i < data.groups.length; i++) {
          var g = data.groups[i];
          var opt = document.createElement('option');
          if (g.group_id == null) continue;
          opt.value = g.group_id;
          opt.textContent = g.group_name || g.group_code || ("\u5c0f\u7ec4 " + g.group_id);
          sel.appendChild(opt);
        }
        // Auto-select first group when loaded
        if (sel.options.length > 1) {
          sel.selectedIndex = 1;
        }
      }
    } catch (e) {
      console.warn('loadGroups error:', e);
    }
  }

  // --- Auto-load when filters change ---
  function setupFilterAutoLoad() {
    var ids = ['session-select', 'group-select', 'timeline-metric', 'window-minutes'];
    for (var i = 0; i < ids.length; i++) {
      var el = document.getElementById(ids[i]);
      if (el) el.addEventListener('change', fetchParticipationData);
    }
  }

  // --- Toggle auto-refresh ---
  function psToggleAutoRefresh(enabled) {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
    if (enabled) {
      fetchParticipationData();
      pollTimer = setInterval(fetchParticipationData, POLL_MS);
    }
  }

  // --- Main fetch (summary + timeline) ---
  async function fetchParticipationData() {
    var groupSel = document.getElementById('group-select');
    var groupId = groupSel ? groupSel.value : '';
    if (!groupId || groupId === 'undefined') {
      showPlaceholder('请选择 Group');
      return;
    }

    var sessionId = document.getElementById('session-select').value;
    var qs = '?window=session&session_id=' + sessionId;
    // Note: NOT passing session_id to participation API (messages may not have session_id set)

    // Fetch summary
    try {
      var summary = await window.fetchJSON('/api/teacher/group/' + groupId + '/participation-summary' + qs);
      if (summary && !summary.error) {
        lastSummary = summary;
        renderGroupSummary(summary);
        var hasData = summary.members && summary.members.length > 0;
        if (hasData) {
          renderMemberBars(summary, getSelectedMetric());
          renderShareBars(summary);
          renderDetailTable(summary);
        } else {
          clearDataAreas();
        }
      }
    } catch (e) {
      console.warn('Summary fetch failed:', e);
    }

    // Fetch timeline
    var tlQs = '?session_id=' + sessionId + '&window_minutes=' + (document.getElementById('window-minutes').value || '5');
    // Using session_id to scope timeline to the selected session
    try {
      var timeline = await window.fetchJSON('/api/teacher/group/' + groupId + '/participation-timeline' + tlQs);
      if (timeline && !timeline.error) {
        lastTimeline = timeline;
        renderTimelineChart();
      } else {
    document.getElementById('timeline-trend-area').innerHTML = '<div class="evidence">暂无参与度数据</div>';
      }
    } catch (e) {
      console.warn('Timeline fetch failed:', e);
    document.getElementById('timeline-trend-area').innerHTML = '<div class="evidence">暂无参与度数据</div>';
    }

    updateTimestamp();
  }

  // --- Show placeholder ---
  function showPlaceholder(msg) {
    var summary = document.getElementById('group-summary-area');
    document.getElementById('member-bars-area').innerHTML = '<div class="evidence">暂无参与度数据</div>';
    document.getElementById('share-bars-area').innerHTML = '<div class="evidence">暂无参与度数据</div>';
    document.getElementById('detail-table-area').innerHTML = '<div class="evidence">暂无参与度数据</div>';
    var timeline = document.getElementById('timeline-trend-area');
    if (summary) summary.innerHTML = '<div class="evidence">暂无参与度数据</div>';

    if (timeline) timeline.innerHTML = '<div class="evidence">' + msg + '</div>';
  }

  function clearDataAreas() {
    document.getElementById('member-bars-area').innerHTML = '<div class="evidence">暂无参与度数据</div>';
    document.getElementById('share-bars-area').innerHTML = '<div class="evidence">暂无参与度数据</div>';
    document.getElementById('detail-table-area').innerHTML = '<div class="evidence">暂无参与度数据</div>';
  }

  // --- Update timestamp ---
  function updateTimestamp() {
    var el = document.getElementById('last-updated');
    if (el) el.innerHTML = '最后更新: ' + window.formatDt(new Date().toISOString());
  }

  // --- Render group summary ---
  function renderGroupSummary(data) {
    var area = document.getElementById('group-summary-area');
    if (!area) return;

    var gm = data.group_metrics || {};
    var groupCode = '';
    var sel = document.getElementById('group-select');
    if (sel && sel.selectedOptions && sel.selectedOptions.length > 0) {
      groupCode = sel.selectedOptions[0].textContent;
    }

    var totalMsgs = 0;
    var totalChars = 0;
    if (data.members) {
      for (var i = 0; i < data.members.length; i++) {
        totalMsgs += data.members[i].message_count || 0;
        totalChars += data.members[i].char_count || 0;
      }
    }

    var levelLabel = '';
    var levelClass = '';
    if (gm.imbalance_level === 'high') { levelLabel = '\u9ad8'; levelClass = 'high'; }
    else if (gm.imbalance_level === 'medium') { levelLabel = '\u4e2d'; levelClass = 'mid'; }
    else { levelLabel = '\u4f4e'; levelClass = 'low'; }

    area.innerHTML =
      '<div class="kpi-grid" style="margin:0">' +
        '<div class="kpi"><span>组别</span><strong>' + window.escapeHtml(groupCode) + '</strong></div>' +
        '<div class="kpi"><span>活跃成员数</span><strong>' + (gm.active_member_count || 0) + '</strong></div>' +
        '<div class="kpi"><span>消息数</span><strong>' + totalMsgs + '</strong></div>' +
        '<div class="kpi"><span>字符数</span><strong>' + totalChars + '</strong></div>' +
        '<div class="kpi"><span>最大消息占比</span><strong>' + window.formatPercent(gm.max_message_share) + '</strong></div>' +
        '<div class="kpi"><span>最小消息占比</span><strong>' + window.formatPercent(gm.min_message_share) + '</strong></div>' +
        '<div class="kpi"><span>Gini 系数</span><strong>' + (typeof gm.gini_coefficient === 'number' ? gm.gini_coefficient.toFixed(4) : '--') + '</strong></div>' +
        '<div class="kpi"><span>不平衡等级</span><strong><span class="badge ' + levelClass + '">' + levelLabel + '</span></strong></div>' +
      '</div>';
  }

  // --- Render member bars (vertical bar chart) ---
  function renderMemberBars(data, metric) {
    var area = document.getElementById('member-bars-area');
    if (!area || !data.members || !data.members.length) {
      if (area) area.innerHTML = '<div class="evidence">暂无参与度数据</div>';
      return;
    }

    var members = data.members;
    var metricLabel = metric === 'char_count' ? '总字数' : (metric === 'active_minutes' ? '活跃分钟数' : '消息数');
    var metricTitle = document.getElementById('member-bars-metric-label');
    if (metricTitle) metricTitle.textContent = metricLabel;

    var maxVal = 0;
    for (var i = 0; i < members.length; i++) {
      var v = members[i][metric] || 0;
      if (v > maxVal) maxVal = v;
    }
    if (maxVal === 0) maxVal = 1;

    var html = '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:12px;align-items:end">';
    for (var j = 0; j < members.length; j++) {
      var m = members[j];
      var val = m[metric] || 0;
      var pct = Math.round(val / maxVal * 100);
      if (pct < 5 && val > 0) pct = 5;
      var color = PARTICIPATION_COLORS[j % PARTICIPATION_COLORS.length];
      html += '<div style="display:flex;flex-direction:column;align-items:center;gap:6px;text-align:center">' +
        '<div style="font-size:var(--text-xs);color:var(--text-muted);font-weight:700">' + val + '</div>' +
        '<div style="width:100%;max-width:60px;height:160px;border-radius:var(--radius-sm);background:var(--border-light);position:relative;overflow:hidden">' +
         '<div style="position:absolute;bottom:0;left:0;right:0;height:' + pct + '%;background:' + color + ';border-radius:var(--radius-sm) var(--radius-sm) 0 0;transition:height 0.3s"></div>' +
        '</div>' +
        '<div style="font-size:var(--text-xs);font-weight:600;word-break:break-all;max-width:100%">' + window.escapeHtml(m.participant_code || ('UID' + m.user_id)) + '</div>' +
      '</div>';
    }
    html += '</div>';
    html += '<div class="muted" style="font-size:11px;margin-top:8px;text-align:center">暂无参与度数据 ' + metricLabel + '</div>';
    area.innerHTML = html;
  }

  // --- Render share bars (horizontal bar chart) ---
  function renderShareBars(data) {
    var area = document.getElementById('share-bars-area');
    if (!area || !data.members || !data.members.length) {
      if (area) area.innerHTML = '<div class="evidence">暂无参与度数据</div>';
      return;
    }

    var members = data.members.slice();
    members.sort(function(a, b) { return (b.message_share || 0) - (a.message_share || 0); });

    var html = '<div style="display:grid;gap:10px">';
    for (var i = 0; i < members.length; i++) {
      var m = members[i];
      var share = m.message_share || 0;
      var pct = Math.round(share * 100);
      var color = PARTICIPATION_COLORS[i % PARTICIPATION_COLORS.length];
      var label = window.escapeHtml(m.participant_code || ('UID' + m.user_id));
      html += '<div style="display:grid;gap:4px">' +
        '<div style="display:flex;justify-content:space-between;font-size:var(--text-xs)">' +
         '<span style="font-weight:600">' + label + '</span>' +
         '<span style="color:var(--text-muted)">' + window.formatPercent(share) + ' (' + m.message_count + ' 条消息)</span>' +
        '</div>' +
        '<div style="height:24px;border-radius:var(--radius-sm);background:var(--border-light);overflow:hidden">' +
         '<div style="height:100%;width:' + pct + '%;background:' + color + ';border-radius:var(--radius-sm);transition:width 0.3s;display:flex;align-items:center;justify-content:center;min-width:0"></div>' +
        '</div>' +
      '</div>';
    }
    html += '</div>';
    area.innerHTML = html;
  }

  // --- Render detail table ---
  function renderDetailTable(data) {
    var area = document.getElementById('detail-table-area');
    if (!area || !data.members || !data.members.length) {
      if (area) area.innerHTML = '<div class="evidence">暂无参与度数据</div>';
      return;
    }

    var members = data.members;
    var html = '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:var(--text-sm)">' +
      '<thead><tr style="background:var(--border-light)">' +
        '<th style="padding:8px 10px;text-align:left;font-weight:700;border-bottom:1px solid var(--border)">组别</th>' +
        '<th style="padding:8px 10px;text-align:right;font-weight:700;border-bottom:1px solid var(--border)">消息数</th>' +
        '<th style="padding:8px 10px;text-align:right;font-weight:700;border-bottom:1px solid var(--border)">字符数</th>' +
        '<th style="padding:8px 10px;text-align:right;font-weight:700;border-bottom:1px solid var(--border)">消息占比</th>' +
        '<th style="padding:8px 10px;text-align:right;font-weight:700;border-bottom:1px solid var(--border)">活跃分钟数</th>' +
      '</tr></thead><tbody>';

    for (var i = 0; i < members.length; i++) {
      var m = members[i];
      var bg = i % 2 === 0 ? '' : 'background:var(--border-light)';
      html += '<tr style="' + bg + '">' +
        '<td style="padding:8px 10px;border-bottom:1px solid var(--border-light);font-weight:600">' + window.escapeHtml(m.participant_code || ('UID' + m.user_id)) + '</td>' +
        '<td style="padding:8px 10px;border-bottom:1px solid var(--border-light);text-align:right">' + (m.message_count || 0) + '</td>' +
        '<td style="padding:8px 10px;border-bottom:1px solid var(--border-light);text-align:right">' + (m.char_count || 0) + '</td>' +
        '<td style="padding:8px 10px;border-bottom:1px solid var(--border-light);text-align:right">' + window.formatPercent(m.message_share) + '</td>' +
        '<td style="padding:8px 10px;border-bottom:1px solid var(--border-light);text-align:right">' + (m.active_minutes || 0) + '</td>' +
      '</tr>';
    }

    html += '</tbody></table></div>';
    area.innerHTML = html;
  }

  // --- Render timeline line chart ---
  function renderTimelineChart() {
    var area = document.getElementById('timeline-trend-area');
    if (!area) return;
    if (!lastTimeline || !lastTimeline.timeline || lastTimeline.timeline.length < 1) {
      area.innerHTML = '<div class="evidence">暂无参与度数据</div>';
      return;
    }

    var metric = getSelectedMetric();
    var metricLabel = metric === 'char_count' ? '总字数' : (metric === 'active_minutes' ? '活跃分钟数' : '消息数');
    var timeline = lastTimeline.timeline;
    var members = lastTimeline.members || [];

    // Build dataset: for each member, collect per-window values
    var memberSeries = {};
    for (var mi = 0; mi < members.length; mi++) {
      var code = members[mi].participant_code;
      memberSeries[code] = [];
    }

    for (var wi = 0; wi < timeline.length; wi++) {
      var win = timeline[wi];
      for (var mj = 0; mj < win.members.length; mj++) {
        var m = win.members[mj];
        var code = m.participant_code;
        if (!memberSeries[code]) {
          memberSeries[code] = [];
        }
        memberSeries[code].push(m[metric] || 0);
      }
    }

    // Collect all values for y-axis scaling
    var allVals = [];
    var codes = Object.keys(memberSeries);
    for (var ci = 0; ci < codes.length; ci++) {
      for (var vi = 0; vi < memberSeries[codes[ci]].length; vi++) {
        allVals.push(memberSeries[codes[ci]][vi]);
      }
    }
    var maxVal = Math.max.apply(null, allVals);
    if (maxVal === 0) maxVal = 1;
    maxVal = Math.ceil(maxVal * 1.2);
    if (maxVal < 1) maxVal = 1;

    // Chart dimensions
    var W = 700, H = 220, PAD = {t: 20, r: 20, b: 40, l: 50};
    var chartW = W - PAD.l - PAD.r;
    var chartH = H - PAD.t - PAD.b;

    var yTicks = 5;
    var svg = '<svg width="100%" viewBox="0 0 ' + W + ' ' + H + '" style="max-height:' + H + 'px;font-family:system-ui,sans-serif">';

    // Background grid
    for (var yi = 0; yi <= yTicks; yi++) {
      var yVal = Math.round(yi * maxVal / yTicks);
      var yPos = PAD.t + chartH - (yVal / maxVal) * chartH;
      svg += '<line x1="' + PAD.l + '" y1="' + yPos.toFixed(1) + '" x2="' + (W - PAD.r) + '" y2="' + yPos.toFixed(1) + '" stroke="#e2e8f0" stroke-width="0.5"/>';
      svg += '<text x="' + (PAD.l - 6) + '" y="' + (yPos + 3).toFixed(1) + '" text-anchor="end" fill="#94a3b8" font-size="10">' + yVal + '</text>';
    }

    // Draw lines
    var xScale = function(idx, len) {
      return PAD.l + (idx / Math.max(len - 1, 1)) * chartW;
    };
    var yScale = function(v) {
      return PAD.t + chartH - (v / maxVal) * chartH;
    };

    for (var ci = 0; ci < codes.length; ci++) {
      var code = codes[ci];
      var values = memberSeries[code];
      var color = PARTICIPATION_COLORS[ci % PARTICIPATION_COLORS.length];
      var len = values.length;

      // Line path
      var pathD = '';
      for (var vi2 = 0; vi2 < len; vi2++) {
        var x = xScale(vi2, len);
        var y = yScale(values[vi2]);
        pathD += (vi2 === 0 ? 'M' : 'L') + x.toFixed(1) + ',' + y.toFixed(1);
      }

      // Area fill
      var areaD = pathD;
      if (len > 0) {
        areaD += ' L' + xScale(len - 1, len).toFixed(1) + ',' + yScale(0).toFixed(1);
        areaD += ' L' + xScale(0, len).toFixed(1) + ',' + yScale(0).toFixed(1) + ' Z';
      }

      svg += '<path d="' + areaD + '" fill="' + color + '" fill-opacity="0.06" stroke="none"/>';
      svg += '<path d="' + pathD + '" fill="none" stroke="' + color + '" stroke-width="1.8" stroke-linejoin="round"/>';

      // Dots
      var maxInSeries = Math.max.apply(null, values);
      for (var di = 0; di < len; di++) {
        if (di === 0 || di === len - 1 || values[di] === maxInSeries) {
          var dx = xScale(di, len);
          var dy = yScale(values[di]);
          svg += '<circle cx="' + dx.toFixed(1) + '" cy="' + dy.toFixed(1) + '" r="2.5" fill="' + color + '" stroke="white" stroke-width="0.8"/>';
        }
      }
    }

    // X-axis labels (first, middle, last)
    var labelIndices = [0, Math.floor((timeline.length - 1) / 2), timeline.length - 1];
    for (var li = 0; li < labelIndices.length; li++) {
      var lIdx = labelIndices[li];
      var lx = xScale(lIdx, timeline.length - 1);
      var label = (timeline[lIdx].window_start || '').slice(5, 19);
      svg += '<text x="' + lx.toFixed(1) + '" y="' + (H - 8) + '" text-anchor="middle" fill="#94a3b8" font-size="9">' + window.escapeHtml(label) + '</text>';
    }

    svg += '</svg>';

    // Legend
    var legend = '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:8px;padding:8px 0;border-top:1px solid var(--border-light)">';
    for (var lci = 0; lci < codes.length; lci++) {
      var code2 = codes[lci];
      var c = PARTICIPATION_COLORS[lci % PARTICIPATION_COLORS.length];
      var lastVal = memberSeries[code2][memberSeries[code2].length - 1] || 0;
      legend += '<span style="display:flex;align-items:center;gap:4px;font-size:11px;color:var(--text-secondary)">' +
        '<span style="width:10px;height:3px;border-radius:2px;background:' + c + '"></span>' +
        window.escapeHtml(code2) +
        ' <span class="muted" style="font-size:10px">(' + lastVal + ' ' + metricLabel + ')</span>' +
        '</span>';
    }
    legend += '</div>';
    legend += '<div class="muted" style="font-size:10px;text-align:center;margin-top:4px">X轴：时间窗口（分钟） | Y轴：' + metricLabel + ' | 每组每位成员数据</div>';

    area.innerHTML = svg + legend;
  }

  function getSelectedMetric() {
    var el = document.getElementById('timeline-metric');
    currentMetric = (el && el.value) || 'message_count';
    return currentMetric;
  }

})();
