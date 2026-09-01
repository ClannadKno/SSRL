// SSRL-ESP T4 Emotion Review page
// Read-only teacher view. It renders normalized state segments, messages,
// silence intervals, participation, and Agent interventions without LLM calls.

(function() {
  'use strict';

  const POLL_MS = 120000;
  let pollTimer = null;
  let lastData = null;
  let activeRole = 'all';
  let latestLoadRequestId = 0;

  const OBSERVING_STATE = 'observing';
  const UNCLASSIFIED_STATE = 'unclassified';
  const SILENCE_STATE = 'negative_silence';
  const PRIMARY_STATE_ORDER = [
    'standard',
    'deep_thinking',
    'execution_progress',
    'constructive_conflict',
    'interpersonal_conflict',
    'confusion',
    'frustration',
    'burnout',
    'off_topic_self_regulated',
    'off_topic_unregulated',
    'perfunctory_detachment',
    'individual_marginalization'
  ];
  const DISPLAY_STATE_ORDER = PRIMARY_STATE_ORDER.concat([
    OBSERVING_STATE,
    UNCLASSIFIED_STATE,
    SILENCE_STATE
  ]);

  const STATE_COLORS = {
    negative_silence: '#4d6172',
    standard: '#16835d',
    deep_thinking: '#2563eb',
    execution_progress: '#0891b2',
    constructive_conflict: '#7c3aed',
    interpersonal_conflict: '#dc2626',
    confusion: '#d97706',
    frustration: '#ea580c',
    burnout: '#7f1d1d',
    off_topic_self_regulated: '#65a30d',
    off_topic_unregulated: '#a16207',
    perfunctory_detachment: '#78716c',
    individual_marginalization: '#9333ea',
    observing: '#dbeafe',
    unclassified: '#d1d5db',
    unknown_sub_state: '#64748b',
    assessment_complete_unconfirmed: '#64748b',
    assessment_failed_unclassified: '#b91c1c'
  };

  const STATE_LABELS = {
    negative_silence: '消极沉默区间',
    standard: '常规协作',
    deep_thinking: '深度思考',
    execution_progress: '执行推进',
    constructive_conflict: '建设性冲突',
    interpersonal_conflict: '人际冲突',
    confusion: '困惑',
    frustration: '挫败',
    burnout: '倦怠',
    off_topic_self_regulated: '跑题后自主拉回',
    off_topic_unregulated: '跑题未拉回',
    perfunctory_detachment: '敷衍脱离',
    individual_marginalization: '个体边缘化',
    observing: '观察中',
    unclassified: '未分类',
    unknown_sub_state: '子状态不确定',
    assessment_complete_unconfirmed: '已完成评估但无确认片段',
    assessment_failed_unclassified: '评估失败/未分类'
  };

  const LEGACY_STATE_MAP = {
    unknown: UNCLASSIFIED_STATE,
    insufficient_evidence: UNCLASSIFIED_STATE,
    participation_imbalance: UNCLASSIFIED_STATE,
    positive_collaboration: UNCLASSIFIED_STATE,
    negative_silence: SILENCE_STATE,
    conflict_tension: UNCLASSIFIED_STATE,
    blocked_frustration: UNCLASSIFIED_STATE,
    frustration_stuck: UNCLASSIFIED_STATE,
    coordination_disorder: UNCLASSIFIED_STATE,
    task_detached: UNCLASSIFIED_STATE,
    off_task: UNCLASSIFIED_STATE,
    conflict_repair: UNCLASSIFIED_STATE,
    positive_recovery: UNCLASSIFIED_STATE,
    psychological_safety_risk: UNCLASSIFIED_STATE,
    high_intensity_overload: UNCLASSIFIED_STATE,
    stage_achievement: UNCLASSIFIED_STATE,
    unknown_sub_state: UNCLASSIFIED_STATE
  };

  const SPEAKER_COLORS = ['#244c7a', '#20745f', '#b36a12', '#8b4f9f', '#4d6172', '#9a4c7b'];

  window.initEmotionTrendPage = async function() {
    bindControls();
    await Promise.all([loadGroups(), loadSessions()]);
    if (document.getElementById('group-select') && document.getElementById('group-select').value) {
      window.loadData();
    } else {
      showPlaceholder('请先选择小组');
    }
    startPolling();
  };

  async function loadGroups() {
    try {
      const data = await window.fetchJSON('/api/teacher/groups?all=true');
      const sel = document.getElementById('group-select');
      if (!sel) return;
      sel.innerHTML = '<option value="">-- 选择小组 --</option>';
      for (const g of data.groups || []) {
        if (g.group_id == null) continue;
        const opt = document.createElement('option');
        opt.value = g.group_id;
        opt.textContent = g.group_name || g.group_code || ('小组 ' + g.group_id);
        sel.appendChild(opt);
      }
      if (sel.options.length > 1 && !sel.value) sel.selectedIndex = 1;
    } catch (e) {
      console.warn('loadGroups error:', e);
    }
  }

  window.loadSessions = async function() {
    try {
      const data = await window.fetchJSON('/api/teacher/sessions?all=true');
      const sel = document.getElementById('session-select');
      if (!sel) return;
      sel.innerHTML = '<option value="">-- 选择课次 --</option>';
      for (const s of data.sessions || []) {
        const opt = document.createElement('option');
        opt.value = s.id;
        opt.textContent = '课次 #' + s.id + ' | 第' + (s.session_no || '?') + ' 课时' + (s.session_role ? ' (' + s.session_role + ')' : '');
        sel.appendChild(opt);
      }
    } catch (e) {
      console.warn('loadSessions error:', e);
    }
  };

  window.reloadGroupsForSession = async function() {
    await loadGroups();
  };

  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(window.loadData, POLL_MS);
  }

  window.etStopPolling = function() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  };

  window.etStartPolling = function() {
    if (!pollTimer) pollTimer = setInterval(window.loadData, POLL_MS);
    window.loadData();
  };

  window.loadData = async function() {
    const requestId = ++latestLoadRequestId;
    const groupEl = document.getElementById('group-select');
    const groupId = groupEl ? groupEl.value : '';
    if (!groupId) {
      showPlaceholder('请先选择小组');
      return;
    }

    const sessionId = document.getElementById('session-select')?.value || '';
    const windowMinutes = document.getElementById('window-minutes')?.value || '1';
    let url = '/api/teacher/group/' + encodeURIComponent(groupId) + '/emotion-review?window_minutes=' + encodeURIComponent(windowMinutes);
    if (sessionId) url += '&session_id=' + encodeURIComponent(sessionId);

    try {
      const data = await window.fetchJSON(url);
      if (requestId !== latestLoadRequestId) return;
      lastData = data || {};
      renderAll(lastData);
      updateTimestamp();
    } catch (e) {
      if (requestId !== latestLoadRequestId) return;
      showError('数据加载失败: ' + (e.message || e));
    }
  };

  function bindControls() {
    ['session-select', 'group-select', 'window-minutes'].forEach(function(id) {
      const el = document.getElementById(id);
      if (el) el.addEventListener('change', window.loadData);
    });

    document.querySelectorAll('.emotion-role-filter').forEach(function(button) {
      button.addEventListener('click', function() {
        activeRole = button.dataset.role || 'all';
        refreshRoleButtons();
        renderMessages();
      });
    });
    refreshRoleButtons();

    const stateFilter = document.getElementById('state-filter');
    if (stateFilter) stateFilter.addEventListener('change', renderMessages);
    const search = document.getElementById('message-search');
    if (search) search.addEventListener('input', renderMessages);
  }

  function refreshRoleButtons() {
    document.querySelectorAll('.emotion-role-filter').forEach(function(button) {
      button.classList.toggle('active', (button.dataset.role || 'all') === activeRole);
    });
  }

  function updateTimestamp() {
    const el = document.getElementById('last-updated');
    if (el) el.textContent = '最后更新: ' + window.formatDt(new Date().toISOString());
  }

  function showPlaceholder(message) {
    ['timeline-area', 'message-flow-area', 'participation-area', 'state-distribution-area', 'intervention-area'].forEach(function(id) {
      const el = document.getElementById(id);
      if (el) el.innerHTML = '<div class="emotion-empty">' + escapeHtml(message) + '</div>';
    });
    const summary = document.getElementById('review-summary');
    if (summary) summary.innerHTML = '';
    const count = document.getElementById('message-count-badge');
    if (count) count.textContent = '0 条';
  }

  function showError(message) {
    ['timeline-area', 'message-flow-area', 'participation-area', 'state-distribution-area', 'intervention-area'].forEach(function(id) {
      const el = document.getElementById(id);
      if (el) el.innerHTML = '<div class="emotion-empty" style="color:var(--danger-text)">' + escapeHtml(message) + '</div>';
    });
    const summary = document.getElementById('review-summary');
    if (summary) summary.innerHTML = '<span class="badge" style="color:var(--danger-text)">加载失败</span>';
  }

  function renderAll(data) {
    const lw = document.getElementById('legacy-warning');
    if (lw) lw.style.display = data.time_range && data.time_range.legacy_data_warning ? 'block' : 'none';
    renderSummary(data);
    registerStateSystem(data);
    renderStateFilter(data);
    renderTimeline(data);
    renderMessages();
    renderParticipation(data);
    renderDistribution(data);
    renderInterventions(data);
  }

  function escapeHtml(value) {
    return window.escapeHtml ? window.escapeHtml(String(value || '')) : String(value || '');
  }

  function normalizeStateCode(code) {
    const raw = String(code || '').trim();
    if (STATE_LABELS[raw]) return raw;
    return LEGACY_STATE_MAP[raw] || UNCLASSIFIED_STATE;
  }

  function stateLabel(code, fallback) {
    const normalized = normalizeStateCode(code);
    if (fallback && normalized === code) return fallback;
    return STATE_LABELS[normalized] || STATE_LABELS[UNCLASSIFIED_STATE];
  }

  function stateColor(code) {
    return STATE_COLORS[normalizeStateCode(code)] || STATE_COLORS[UNCLASSIFIED_STATE];
  }

  function registerStateSystem(data) {
    (data.detailed_state_system || data.state_system || []).forEach(function(item) {
      if (!item || !item.code || !item.label) return;
      STATE_LABELS[item.code] = item.label;
    });
  }

  function orderedUnique(codes) {
    const wanted = new Set((codes || []).map(normalizeStateCode));
    const ordered = DISPLAY_STATE_ORDER.filter(function(code) {
      return wanted.has(code);
    });
    wanted.forEach(function(code) {
      if (!ordered.includes(code) && code !== OBSERVING_STATE && code !== UNCLASSIFIED_STATE) {
        ordered.push(code);
      }
    });
    return ordered;
  }

  function stateLaneOrder(data) {
    const present = (data.state_segments || []).map(function(segment) {
      return segment.state_code;
    });
    if ((data.silence_segments || []).length) present.push('negative_silence');
    return orderedUnique(present);
  }

  function compactTime(value) {
    if (!value) return '--';
    const text = String(value);
    if (text.length >= 16) return escapeHtml(text.slice(5, 16));
    return escapeHtml(text);
  }

  function pad2(value) {
    return String(value).padStart(2, '0');
  }

  function compactLocalMs(value) {
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return '--';
    return pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' +
      pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  function timeMs(value) {
    if (!value) return null;
    const d = new Date(String(value).replace(' ', 'T'));
    if (Number.isNaN(d.getTime())) return null;
    return d.getTime();
  }

  function formatNumber(value) {
    if (value == null || value === '') return '--';
    const n = Number(value);
    if (Number.isNaN(n)) return '--';
    return n.toFixed(2);
  }

  function formatDuration(seconds) {
    const value = Math.max(0, Number(seconds || 0));
    if (value < 60) return Math.round(value) + ' 秒';
    const minutes = value / 60;
    if (minutes < 60) {
      return (Math.round(minutes * 10) / 10).toString().replace(/\.0$/, '') + ' 分钟';
    }
    const hours = minutes / 60;
    return (Math.round(hours * 10) / 10).toString().replace(/\.0$/, '') + ' 小时';
  }

  function renderSummary(data) {
    const el = document.getElementById('review-summary');
    if (!el) return;
    const s = data.summary || {};
    const current = data.current_state || {};
    const currentStatus = current.assessment_status || s.latest_assessment_status || UNCLASSIFIED_STATE;
    const latestState = currentStatus === 'confirmed'
      ? stateLabel(current.semantic_state || s.latest_state_code, current.state_label || s.latest_state_label)
      : (STATE_LABELS[currentStatus] || STATE_LABELS[UNCLASSIFIED_STATE]);
    const items = [
      '消息 ' + (s.message_count || 0),
      '学生 ' + (s.active_student_count || 0),
      'Agent ' + (s.agent_message_count || 0),
      '状态片段 ' + (s.state_segment_count || 0),
      '观察中 ' + (s.observing_student_message_count || 0),
      '未分类 ' + (s.unclassified_student_message_count || 0),
      '沉默区间 ' + (s.silence_segment_count || 0),
      '介入 ' + (s.intervention_count || 0),
      '最新 ' + latestState
    ];
    el.innerHTML = items.map(function(item) {
      return '<span class="badge">' + escapeHtml(item) + '</span>';
    }).join('');
  }

  function renderStateFilter(data) {
    const select = document.getElementById('state-filter');
    if (!select) return;
    const current = select.value || 'all';
    const configured = (data.detailed_state_system || data.state_system || [])
      .map(function(item) { return item && item.code; })
      .filter(Boolean);
    const filters = orderedUnique(
      configured.concat(PRIMARY_STATE_ORDER, [
        OBSERVING_STATE,
        UNCLASSIFIED_STATE,
        SILENCE_STATE
      ])
    );
    select.innerHTML = '<option value="all">全部状态</option>' +
      filters.map(function(code) {
        return '<option value="' + escapeHtml(code) + '">' + escapeHtml(stateLabel(code)) + '</option>';
      }).join('');
    if (current && Array.from(select.options).some(function(opt) { return opt.value === current; })) {
      select.value = current;
    }
  }

  function timelineBounds(data) {
    const points = [];
    if (data.time_range) {
      points.push(timeMs(data.time_range.start), timeMs(data.time_range.end));
    }
    (data.state_segments || []).forEach(function(s) {
      points.push(timeMs(s.start_at), timeMs(s.end_at));
    });
    (data.silence_segments || []).forEach(function(s) {
      points.push(timeMs(s.start_at), timeMs(s.end_at));
    });
    (data.messages || []).forEach(function(m) {
      points.push(timeMs(m.created_at));
    });
    const valid = points.filter(function(v) { return Number.isFinite(v); });
    if (!valid.length) {
      const now = Date.now();
      return { start: now - 60 * 60 * 1000, end: now };
    }
    const start = Math.min.apply(null, valid);
    let end = Math.max.apply(null, valid);
    if (end <= start) end = start + 60 * 1000;
    return { start: start, end: end };
  }

  function pos(value, bounds) {
    const t = timeMs(value);
    if (!Number.isFinite(t)) return 0;
    return Math.max(0, Math.min(100, ((t - bounds.start) / (bounds.end - bounds.start)) * 100));
  }

  function chooseTickStepMinutes(durationMs) {
    const rough = Math.max(1, Math.ceil((durationMs / 60000) / 8));
    const steps = [1, 2, 5, 10, 15, 30, 60, 120, 240, 720, 1440];
    for (const step of steps) {
      if (step >= rough) return step;
    }
    return steps[steps.length - 1];
  }

  function renderTimeAxis(bounds) {
    const step = chooseTickStepMinutes(bounds.end - bounds.start) * 60 * 1000;
    const first = Math.ceil(bounds.start / step) * step;
    const ticks = [];
    for (let ts = first; ts <= bounds.end; ts += step) {
      ticks.push(ts);
      if (ticks.length > 14) break;
    }
    if (!ticks.length || ticks[0] > bounds.start) ticks.unshift(bounds.start);
    if (ticks[ticks.length - 1] < bounds.end) ticks.push(bounds.end);
    return ticks.map(function(ts) {
      const left = Math.max(0, Math.min(100, ((ts - bounds.start) / (bounds.end - bounds.start)) * 100));
      return '<div class="emotion-tick major" style="left:' + left.toFixed(2) + '%"><span>' + compactLocalMs(ts) + '</span></div>';
    }).join('');
  }

  function segmentTitle(segment) {
    const evidence = (segment.evidence_message_ids || []).map(function(id) { return '#' + id; }).join(', ') || '无';
    return [
      segment.state_label || stateLabel(segment.state_code),
      compactTime(segment.start_at) + ' - ' + compactTime(segment.end_at),
      '证据 ' + evidence,
      '置信 ' + formatNumber(segment.confidence),
      '来源 ' + (segment.source || '--')
    ].join(' | ');
  }

  function silenceTitle(segment) {
    return [
      '消极沉默区间',
      compactTime(segment.start_at) + ' - ' + compactTime(segment.end_at),
      '上一学生消息 #' + (segment.previous_student_message_id || '--'),
      '下一学生消息 #' + (segment.next_student_message_id || '--'),
      formatDuration(segment.duration_seconds || segment.gap_seconds || 0)
    ].join(' | ');
  }

  function renderTimeline(data) {
    const area = document.getElementById('timeline-area');
    if (!area) return;
    const stateSegments = data.state_segments || [];
    const silenceSegments = data.silence_segments || [];
    const messages = data.messages || [];
    if (!stateSegments.length && !silenceSegments.length && !messages.length) {
      area.innerHTML = '<div class="emotion-empty">暂无数据</div>';
      return;
    }

    const bounds = timelineBounds(data);
    const laneOrder = stateLaneOrder(data);
    let html = '<div class="emotion-timeline">';
    html += '<div class="emotion-axis">' + renderTimeAxis(bounds) + '</div>';
    html += '<div class="emotion-state-band segmented">';

    laneOrder.forEach(function(code) {
      html += '<div class="emotion-state-lane">';
      html += '<div class="emotion-lane-name"><span class="emotion-swatch" style="background:' + stateColor(code) + '"></span>' + escapeHtml(stateLabel(code)) + '</div>';
      html += '<div class="emotion-lane-body">';
      if (code === 'negative_silence') {
        silenceSegments.forEach(function(segment) {
          const left = pos(segment.start_at, bounds);
          let right = pos(segment.end_at || segment.start_at, bounds);
          if (right <= left) right = Math.min(100, left + 1.4);
          const width = Math.max(1.4, right - left);
          html += '<button type="button" class="emotion-state-segment silence" title="' + escapeHtml(silenceTitle(segment)) + '" data-sequence="' + escapeHtml(segment.previous_student_message_id || '') + '" style="left:' + left.toFixed(2) + '%;width:' + width.toFixed(2) + '%;background:' + stateColor(code) + '"><span>' + escapeHtml(formatDuration(segment.duration_seconds || segment.gap_seconds || 0)) + '</span></button>';
        });
      } else {
        stateSegments.filter(function(segment) {
          return normalizeStateCode(segment.state_code) === code;
        }).forEach(function(segment) {
          const left = pos(segment.start_at, bounds);
          let right = pos(segment.end_at || segment.start_at, bounds);
          if (right <= left) right = Math.min(100, left + 1.4);
          const width = Math.max(1.4, right - left);
          const label = width < 6 ? ('#' + segment.start_message_id) : (segment.state_label || stateLabel(code));
          html += '<button type="button" class="emotion-state-segment" title="' + escapeHtml(segmentTitle(segment)) + '" data-sequence="' + escapeHtml(segment.start_message_id || '') + '" style="left:' + left.toFixed(2) + '%;width:' + width.toFixed(2) + '%;background:' + stateColor(code) + '"><span>' + escapeHtml(label) + '</span></button>';
        });
      }
      html += '</div></div>';
    });
    html += '</div>';

    html += '<div class="emotion-agent-lane"><span class="emotion-lane-label">Agent 介入</span>';
    const interventionBySeq = {};
    (data.interventions || []).forEach(function(item) {
      if (item.linked_sequence != null) interventionBySeq[item.linked_sequence] = item;
    });
    messages.filter(function(m) { return m.role === 'agent'; }).forEach(function(m) {
      const related = interventionBySeq[m.sequence] || null;
      const kind = agentMessageKind(m, related);
      const markerClass = agentMarkerClass(kind);
      const title = '#' + (m.sequence || m.id) + ' ' + agentMessageLabel(m, related) + ' | ' + (related ? (related.trigger_type || '') + ' | ' : '') + m.content;
      html += '<button type="button" class="emotion-agent-marker ' + markerClass + '" data-sequence="' + escapeHtml(m.sequence || m.id) + '" title="' + escapeHtml(title) + '" style="left:' + pos(m.created_at, bounds).toFixed(2) + '%"></button>';
    });
    html += '</div>';

    html += '<div class="emotion-legend">';
    laneOrder.forEach(function(code) {
      html += '<span class="emotion-legend-item"><span class="emotion-swatch" style="background:' + stateColor(code) + '"></span>' + escapeHtml(stateLabel(code)) + '</span>';
    });
    html += '<span class="emotion-legend-item"><span class="emotion-swatch" style="background:var(--success-text)"></span>策略智能体 · 自动介入</span>';
    html += '<span class="emotion-legend-item"><span class="emotion-swatch" style="background:var(--warning-text)"></span>策略智能体 · 学生求助</span>';
    html += '<span class="emotion-legend-item"><span class="emotion-swatch" style="background:var(--primary)"></span>情绪智能体</span>';
    html += '<span class="emotion-legend-item"><span class="emotion-swatch" style="background:var(--text-muted)"></span>教师介入</span>';
    html += '</div>';

    const warnings = data.quality_warnings || [];
    if (warnings.length) {
      html += '<div class="emotion-review-note">' + escapeHtml(warnings.length + ' 条状态片段质量提示，已使用固定优先级解析。') + '</div>';
    }
    html += '</div>';

    area.innerHTML = html;
    area.querySelectorAll('.emotion-agent-marker, .emotion-state-segment').forEach(function(marker) {
      marker.addEventListener('click', function() {
        if (marker.dataset.sequence) scrollToMessage(marker.dataset.sequence);
      });
    });
  }

  function agentMessageKind(message, item) {
    const explicit = ((message && message.agent_message_kind) || (item && item.intervention_kind) || '').toLowerCase();
    if (explicit) return explicit;
    const source = ((item && (item.trigger_type || item.trigger_source)) || (message && message.agent_trigger_source) || '').toLowerCase();
    const mode = ((item && item.push_mode) || '').toLowerCase();
    const agentType = ((message && message.agent_type) || '').toLowerCase();
    if (agentType === 'emotion') return 'emotion';
    if (source.indexOf('student_help') !== -1 || mode.indexOf('student') !== -1) return 'strategy_student_help';
    if (source.indexOf('teacher') !== -1 || mode.indexOf('teacher') !== -1) return 'strategy_teacher';
    if (source || mode.indexOf('auto') !== -1 || mode.indexOf('sera_auto') !== -1 || agentType === 'strategy' || item) return 'strategy_auto';
    return 'legacy_agent';
  }

  function agentMarkerClass(kind) {
    if (kind === 'strategy_student_help') return 'help';
    if (kind === 'strategy_auto') return 'auto';
    if (kind === 'strategy_teacher') return 'formal';
    if (kind === 'emotion') return 'ordinary';
    return 'ordinary';
  }

  function agentMessageLabel(message, item) {
    const explicit = (message && message.agent_display_label) || (item && item.display_label);
    if (explicit) return explicit;
    const kind = agentMessageKind(message, item);
    if (kind === 'emotion') return '情绪智能体';
    if (kind === 'strategy_student_help') return '策略智能体 · 学生求助';
    if (kind === 'strategy_teacher') return '教师介入';
    if (kind === 'strategy_auto') return '策略智能体 · 自动介入';
    return 'Agent · legacy/未知来源';
  }

  function messageMatches(message) {
    const stateFilter = document.getElementById('state-filter')?.value || 'all';
    const search = (document.getElementById('message-search')?.value || '').trim().toLowerCase();
    if (stateFilter === 'negative_silence') return false;
    if (activeRole !== 'all' && message.role !== activeRole) return false;
    if (stateFilter !== 'all') {
      if (message.role !== 'student') return false;
      if (stateFilter === OBSERVING_STATE || stateFilter === UNCLASSIFIED_STATE) {
        if ((message.assessment_status || UNCLASSIFIED_STATE) !== stateFilter) return false;
      } else if (
        message.assessment_status !== 'confirmed' ||
        normalizeStateCode(message.semantic_state || message.state_code) !== stateFilter
      ) return false;
    }
    if (search && String(message.content || '').toLowerCase().indexOf(search) === -1) return false;
    return true;
  }

  function silenceMatches(segment) {
    const stateFilter = document.getElementById('state-filter')?.value || 'all';
    const search = (document.getElementById('message-search')?.value || '').trim().toLowerCase();
    if (stateFilter !== 'negative_silence') return false;
    if (activeRole === 'agent') return false;
    const haystack = [
      '消极沉默',
      segment.start_at,
      segment.end_at,
      segment.previous_student_message_id,
      segment.next_student_message_id
    ].join(' ').toLowerCase();
    if (search && haystack.indexOf(search) === -1) return false;
    return true;
  }

  function messageStateTitle(message) {
    const evidence = (message.state_evidence_sequences || message.state_evidence_message_ids || [])
      .map(function(sequence) { return '#' + sequence; })
      .join(', ') || '无';
    return [
      message.display_state_label || message.state_label || stateLabel(message.display_state_code || message.semantic_state || message.state_code),
      '置信 ' + formatNumber(message.state_confidence),
      '证据 ' + evidence,
      '来源 ' + (message.state_source || '--')
    ].join(' | ');
  }

  function renderMessages() {
    const area = document.getElementById('message-flow-area');
    if (!area) return;
    const data = lastData || {};
    const messages = data.messages || [];
    const stateFilter = document.getElementById('state-filter')?.value || 'all';

    if (stateFilter === 'negative_silence') {
      const visibleSilence = (data.silence_segments || []).filter(silenceMatches);
      const badge = document.getElementById('message-count-badge');
      if (badge) badge.textContent = visibleSilence.length + '/' + (data.silence_segments || []).length + ' 个区间';
      if (!visibleSilence.length) {
        area.innerHTML = '<div class="emotion-empty">没有匹配的沉默区间</div>';
        return;
      }
      area.innerHTML = visibleSilence.map(function(segment) {
        return '<article class="emotion-message silence-event" data-sequence="' + escapeHtml(segment.previous_student_message_id || '') + '">' +
          '<div class="seq">#' + escapeHtml(segment.previous_student_message_id || '--') + '</div>' +
          '<div class="time">' + compactTime(segment.start_at).slice(-5) + '</div>' +
          '<div class="speaker">时间区间</div>' +
          '<div class="state-cell"><span class="emotion-chip" style="background:' + stateColor(SILENCE_STATE) + '">消极沉默区间</span></div>' +
          '<div class="content">' + escapeHtml(compactTime(segment.start_at) + ' - ' + compactTime(segment.end_at) + ' · ' + formatDuration(segment.duration_seconds || segment.gap_seconds || 0)) + '</div>' +
          '</article>';
      }).join('');
      return;
    }

    const visible = messages.filter(messageMatches);
    const badge = document.getElementById('message-count-badge');
    if (badge) badge.textContent = visible.length + '/' + messages.length + ' 条';

    if (!visible.length) {
      area.innerHTML = '<div class="emotion-empty">没有匹配的消息</div>';
      return;
    }

    area.innerHTML = visible.map(function(m) {
      const isAgent = m.role === 'agent';
      const isTeacher = m.role === 'teacher';
      let stateCell = '<span class="emotion-state-empty" aria-label="无学生协作状态"></span>';
      if (isAgent) {
        stateCell = '<span class="emotion-chip" style="background:#244c7a">' + escapeHtml(agentMessageLabel(m)) + '</span>';
      } else if (isTeacher) {
        stateCell = '<span class="emotion-chip" style="background:#4b5563">教师</span>';
      } else {
        const assessmentStatus = m.assessment_status || (m.state_code ? 'confirmed' : UNCLASSIFIED_STATE);
        const displayState = m.display_state_code || m.semantic_state || m.state_code || assessmentStatus;
        const displayLabel = m.display_state_label || m.state_label || stateLabel(displayState);
        if (assessmentStatus === 'confirmed') {
          const semanticState = m.semantic_state || m.state_code;
          const label = displayLabel;
          stateCell = '<span class="emotion-chip confirmed" title="' + escapeHtml(messageStateTitle(m)) + '" style="background:' + stateColor(semanticState) + '">' + escapeHtml(label) + '</span>';
        } else if (assessmentStatus === OBSERVING_STATE) {
          stateCell = '<span class="emotion-chip observing" title="等待新的已确认状态片段">' + escapeHtml(displayLabel) + '</span>';
        } else {
          const detail = [m.state_assignment_reason, m.error_code].filter(Boolean).join(' · ');
          stateCell = '<span class="emotion-chip unclassified" title="' + escapeHtml(detail || '无已确认主子状态') + '" style="background:' + stateColor(displayState) + '">' + escapeHtml(displayLabel) + '</span>';
        }
      }
      return '<article class="emotion-message ' + (isAgent ? 'agent' : '') + ' status-' + escapeHtml(m.assessment_status || '') + '" data-sequence="' + escapeHtml(m.sequence || m.id) + '">' +
        '<div class="seq">#' + escapeHtml(m.sequence || m.id) + '</div>' +
        '<div class="time">' + compactTime(m.created_at).slice(-5) + '</div>' +
        '<div class="speaker">' + escapeHtml(m.display_name || '-') + '</div>' +
        '<div class="state-cell">' + stateCell + '</div>' +
        '<div class="content">' + escapeHtml(m.content || '') + '</div>' +
        '</article>';
    }).join('');
  }

  function scrollToMessage(sequence) {
    const selector = '.emotion-message[data-sequence="' + CSS.escape(String(sequence)) + '"]';
    let row = document.querySelector(selector);
    if (!row) {
      activeRole = 'all';
      refreshRoleButtons();
      const stateFilter = document.getElementById('state-filter');
      if (stateFilter) stateFilter.value = 'all';
      renderMessages();
      row = document.querySelector(selector);
    }
    if (row) {
      row.scrollIntoView({ behavior: 'smooth', block: 'center' });
      row.animate([{ outline: '2px solid var(--primary)' }, { outline: '2px solid transparent' }], { duration: 1200 });
    }
  }

  function renderParticipation(data) {
    const area = document.getElementById('participation-area');
    if (!area) return;
    const items = data.participation || [];
    const timeline = data.participation_timeline || [];
    if (!items.length && !timeline.length) {
      area.innerHTML = '<div class="emotion-empty">暂无学生消息</div>';
      return;
    }
    const colors = {};
    items.forEach(function(item, index) {
      colors[item.display_name] = SPEAKER_COLORS[index % SPEAKER_COLORS.length];
    });
    const max = Math.max.apply(null, items.map(function(item) { return item.message_count || 0; })) || 1;
    let html = '<div class="participation-summary">' + items.map(function(item, index) {
      const width = Math.max(4, ((item.message_count || 0) / max) * 100);
      const color = colors[item.display_name] || SPEAKER_COLORS[index % SPEAKER_COLORS.length];
      return '<div class="emotion-bar-row">' +
        '<div title="' + escapeHtml(item.display_name) + '">' + escapeHtml(item.display_name) + '</div>' +
        '<div class="emotion-bar-track"><div class="emotion-bar-fill" style="width:' + width.toFixed(1) + '%;background:' + color + '"></div></div>' +
        '<div class="seq">' + (item.message_count || 0) + '</div>' +
        '</div>';
    }).join('') + '</div>';

    html += '<div class="emotion-bars">';
    timeline.forEach(function(bucket) {
      const total = Number(bucket.student_message_count || 0);
      const active = (bucket.students || []).filter(function(student) {
        return Number(student.message_count || 0) > 0;
      });
      const segments = active.map(function(student, index) {
        const share = total ? (Number(student.message_count || 0) / total) * 100 : 0;
        const color = colors[student.display_name] || SPEAKER_COLORS[index % SPEAKER_COLORS.length];
        const title = compactTime(bucket.bucket_start) + ' · ' + student.display_name + ' · ' + student.message_count + ' 条';
        return '<span class="participation-minute-segment" title="' + escapeHtml(title) + '" style="width:' + share.toFixed(1) + '%;background:' + color + '"></span>';
      }).join('');
      html += '<div class="participation-minute-row" data-sequence="' + escapeHtml(bucket.first_sequence || '') + '">' +
        '<div class="participation-minute-time">' + compactTime(bucket.bucket_start).slice(-5) + '</div>' +
        '<div class="participation-minute-track">' + (segments || '<span class="participation-minute-empty"></span>') + '</div>' +
        '<div class="participation-minute-count">' + total + '</div>' +
        '</div>';
    });
    html += '</div>';

    html += '<div class="participation-legend">' + items.map(function(item) {
      const color = colors[item.display_name] || SPEAKER_COLORS[0];
      return '<span class="participation-legend-item"><span class="emotion-swatch" style="background:' + color + '"></span>' + escapeHtml(item.display_name) + '</span>';
    }).join('') + '</div>';
    area.innerHTML = html;
    area.querySelectorAll('.participation-minute-row[data-sequence]').forEach(function(row) {
      row.addEventListener('click', function() {
        if (row.dataset.sequence) scrollToMessage(row.dataset.sequence);
      });
    });
  }

  function normalizeDistribution(rawDist, order) {
    const dist = {};
    order.forEach(function(code) {
      dist[code] = {
        state_code: code,
        state_label: stateLabel(code),
        segment_count: 0,
        message_count: 0,
        duration_seconds: 0
      };
    });
    Object.keys(rawDist || {}).forEach(function(code) {
      const normalized = normalizeStateCode(code);
      if (!dist[normalized]) return;
      const value = rawDist[code];
      if (typeof value === 'number') {
        dist[normalized].segment_count += Number(value || 0);
      } else {
        dist[normalized].segment_count += Number(value.segment_count || 0);
        dist[normalized].message_count += Number(value.message_count || 0);
        dist[normalized].duration_seconds += Number(value.duration_seconds || 0);
      }
    });
    return dist;
  }

  function renderDistribution(data) {
    const area = document.getElementById('state-distribution-area');
    if (!area) return;
    const rawDist = data.detailed_distribution || data.distribution || {};
    const order = PRIMARY_STATE_ORDER.concat([
      OBSERVING_STATE,
      UNCLASSIFIED_STATE
    ]).filter(function(code) {
      const item = rawDist[code] || {};
      return Number(item.segment_count || 0) > 0 ||
        Number(item.message_count || 0) > 0;
    });
    const silenceSegments = data.silence_segments || [];
    if (!order.length && !silenceSegments.length) {
      area.innerHTML = '<div class="emotion-empty">暂无已确认状态片段</div>';
      return;
    }
    const dist = normalizeDistribution(rawDist, order);
    const max = order.length ? (Math.max.apply(null, order.map(function(code) {
      return Math.max(
        Number(dist[code].segment_count || 0),
        Number(dist[code].message_count || 0)
      );
    })) || 1) : 1;
    let html = '<div class="emotion-bars">';
    order.forEach(function(code) {
      const item = dist[code];
      const count = Number(item.segment_count || 0);
      const messageCount = Number(item.message_count || 0);
      const widthValue = Math.max(count, messageCount);
      const width = widthValue ? Math.max(4, (widthValue / max) * 100) : 0;
      const details = count + ' 个片段 · ' + messageCount +
        ' 条学生消息 · ' + formatDuration(item.duration_seconds || 0);
      html += '<div class="emotion-bar-row distribution-row">' +
        '<div title="' + escapeHtml(stateLabel(code)) + '">' + escapeHtml(stateLabel(code)) + '</div>' +
        '<div class="emotion-bar-track"><div class="emotion-bar-fill" style="width:' + width.toFixed(1) + '%;background:' + stateColor(code) + '"></div></div>' +
        '<div class="seq">' + count + '</div>' +
        '<div class="distribution-detail">' + escapeHtml(details) + '</div>' +
        '</div>';
    });
    html += '</div>';
    if (silenceSegments.length) {
      const silenceSeconds = silenceSegments.reduce(function(total, item) {
        return total + Number(item.duration_seconds || item.gap_seconds || 0);
      }, 0);
      html += '<div class="emotion-review-note"><strong>独立沉默时间区间：</strong>' +
        escapeHtml(silenceSegments.length + ' 个 · ' + formatDuration(silenceSeconds)) +
        '</div>';
    }
    if (data.summary && data.summary.duration_note) {
      html += '<div class="emotion-review-note">' + escapeHtml(data.summary.duration_note) + '</div>';
    }
    area.innerHTML = html;
  }

  function renderInterventions(data) {
    const area = document.getElementById('intervention-area');
    if (!area) return;
    const items = data.interventions || [];
    if (!items.length) {
      area.innerHTML = '<div class="emotion-empty">暂无介入记录</div>';
      return;
    }
    area.innerHTML = '<div class="emotion-interventions">' + items.map(function(item) {
      const seq = item.linked_sequence ? ' #' + item.linked_sequence : '';
      const label = item.display_label || 'Agent 介入';
      const title = compactTime(item.created_at) + seq + ' · ' + label;
      const details = [
        item.actor,
        item.trigger_type,
        item.strategy_id,
        item.auto_uptake_type || item.manual_uptake_type
      ].filter(Boolean).join(' · ');
      return '<button type="button" class="emotion-intervention" data-sequence="' + escapeHtml(item.linked_sequence || '') + '">' +
        '<strong>' + escapeHtml(title) + '</strong>' +
        '<div class="muted" style="font-size:11px;margin-bottom:4px">' + escapeHtml(details || '未记录策略元数据') + '</div>' +
        '<div>' + escapeHtml(item.message || '') + '</div>' +
        '</button>';
    }).join('') + '</div>';

    area.querySelectorAll('.emotion-intervention').forEach(function(node) {
      node.addEventListener('click', function() {
        if (node.dataset.sequence) scrollToMessage(node.dataset.sequence);
      });
    });
  }
})();
