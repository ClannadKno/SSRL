// SSRL-ESP Teacher Agent Audit (T5) - Full JS implementation
// Requires: teacher-api.js (window.fetchJSON)

(function () {
  "use strict";

 var state = {
    unblinded: true,
   sessions: [],
   groups: [],
   tasks: [],
   strategies: [],
   auditData: null,
   selectedEvent: null,
   highlightedEvidenceSequences: [],
   currentGroupId: null,
   currentSessionId: null,
   correctionTargetId: null,
   correctionInterventionId: null,
 };

  const UPTAKE_TYPES = [
    "ignored", "acknowledged", "discussed",
    "adopted", "adapted", "rejected",
  ];

  const UPTAKE_LABELS = {
    ignored: "忽略",
    acknowledged: "知晓",
    discussed: "讨论",
    adopted: "采纳",
    adapted: "调整后采纳",
    rejected: "拒绝",
  };

  const EVENT_TYPE_LABELS = {
    strategy_pipeline: "三阶段审计",
    assessment_batch: "状态评估批次",
    detector_output: "检测器输出",
    gate_decision: "门控决策",
    strategy_review: "策略复核",
    intervention: "干预事件",
    uptake: "采纳记录",
    autonomous_regulation: "自主调节",
    manual_correction: "人工校正",
    unblind_event: "审计查看事件",
  };

  const EVENT_DOT_CLASSES = {
    strategy_pipeline: "review",
    assessment_batch: "detector",
    detector_output: "detector",
    gate_decision: "gate",
    strategy_review: "review",
    intervention: "intervention",
    uptake: "uptake",
    autonomous_regulation: "regulation",
    manual_correction: "correction",
    unblind_event: "unblind",
  };

  const STATE_ORDER = [
    "standard",
    "deep_thinking",
    "execution_progress",
    "constructive_conflict",
    "interpersonal_conflict",
    "confusion",
    "frustration",
    "burnout",
    "off_topic_self_regulated",
    "off_topic_unregulated",
    "perfunctory_detachment",
    "individual_marginalization",
    "observing",
    "unclassified",
  ];

  const STATE_LABELS = {
    standard: "常规协作",
    deep_thinking: "深度思考",
    execution_progress: "执行推进",
    constructive_conflict: "建设性冲突",
    interpersonal_conflict: "人际性冲突",
    confusion: "困惑",
    frustration: "挫败",
    burnout: "倦怠",
    off_topic_self_regulated: "跑题已自调节",
    off_topic_unregulated: "跑题未自调节",
    perfunctory_detachment: "敷衍脱离",
    individual_marginalization: "个体边缘化",
    observing: "观察中",
    unclassified: "未分类",
  };

  const COARSE_STATE_LABELS = {
    positive_collaboration: "积极协作",
    negative_silence: "消极沉默",
    conflict_tension: "紧张冲突",
    blocked_frustration: "挫败卡住",
    task_detached: "任务脱离",
    unknown: "未知",
  };

  function normalizeStateCode(code) {
    var text = String(code || "").trim();
    if (STATE_ORDER.indexOf(text) >= 0) return text;
    return text ? "unclassified" : "";
  }

  function finalStateCode(row) {
    if (!row) return "";
    return normalizeStateCode(
      row.final_sub_state_code ||
      row.canonical_sub_state_code ||
      row.assessment_status
    );
  }

  function coarseStateCode(row) {
    if (!row) return "";
    return String(
      row.coarse_state_code ||
      row.stage1_state_code ||
      (row.audit_state && row.audit_state.coarse_state_code) ||
      row.fused_state_code ||
      row.state_code ||
      row.detected_state ||
      ""
    ).trim();
  }

  function formatCoarseStateLabel(code) {
    var normalized = String(code || "").trim();
    return normalized
      ? (COARSE_STATE_LABELS[normalized] || normalized) + " (" + normalized + ")"
      : "";
  }

  function formatStateLabel(code) {
    var normalized = normalizeStateCode(code) || "unclassified";
    return (STATE_LABELS[normalized] || "未分类") + " (" + normalized + ")";
  }

  function formatPreciseStateLabel(code) {
    if (!code || code === "历史数据未记录") return code;
    return formatStateLabel(code);
  }

  function renderFieldGrid(fields) {
    var visible = fields.filter(function (f) { return f.value != null && f.value !== ""; });
    if (!visible.length) return "";
    return (
      '<div class="detail-grid">' +
      visible
        .map(function (f) {
          return (
            '<div class="detail-field"><span class="detail-label">' +
            window.escapeHtml(f.label) +
            '</span><span class="detail-value">' +
            window.escapeHtml(String(f.value)) +
            "</span></div>"
          );
        })
        .join("") +
      "</div>"
    );
  }

  function renderTags(title, tags) {
    if (!tags || !tags.length) return "";
    return (
      '<div style="margin-top:12px"><span class="detail-label" style="font-size:11px;color:var(--text-muted);font-weight:700">' +
      window.escapeHtml(title) +
      "</span><div style=\"display:flex;gap:6px;flex-wrap:wrap;margin-top:6px\">" +
      tags.map(function (tag) {
        return '<span class="badge low">' + window.escapeHtml(String(tag)) + "</span>";
      }).join("") +
      "</div></div>"
    );
  }

  function renderScoreTable(scores) {
    if (!scores || !Object.keys(scores).length) return "";
    var rows = Object.keys(scores).sort().map(function (key) {
      return (
        '<div class="detail-field"><span class="detail-label">' +
        window.escapeHtml(key) +
        '</span><span class="detail-value">' +
        window.escapeHtml(String(scores[key])) +
        "</span></div>"
      );
    }).join("");
    return (
      '<div style="margin-top:12px"><span class="detail-label" style="font-size:11px;color:var(--text-muted);font-weight:700">Candidate Scores</span>' +
      '<div class="detail-grid" style="margin-top:6px">' + rows + "</div></div>"
    );
  }

  function renderJsonBlock(title, value) {
    if (value == null || value === "") return "";
    var text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
    return (
      '<div style="margin-top:12px"><span class="detail-label" style="font-size:11px;color:var(--text-muted);font-weight:700">' +
      window.escapeHtml(title) +
      '</span><div class="evidence" style="margin-top:4px;white-space:pre-wrap">' +
      window.escapeHtml(text) +
      "</div></div>"
    );
  }

  function normalizeAgentKind(item) {
    return String((item && (item.agent_message_kind || item.intervention_kind)) || "").trim().toLowerCase();
  }

  function normalizeAgentType(item) {
    var kind = normalizeAgentKind(item);
    if (kind === "emotion") return "emotion";
    if (kind.indexOf("strategy_") === 0) return "strategy";
    var agentType = String((item && item.agent_type) || "").toLowerCase();
    if (agentType === "emotion") return "emotion";
    if (agentType === "strategy") return "strategy";
    return "";
  }

  function agentKindLabel(kind) {
    return {
      emotion: "情绪智能体",
      strategy_auto: "策略智能体 · 自动介入",
      strategy_student_help: "策略智能体 · 学生求助",
      strategy_teacher: "策略智能体 · 教师介入",
      legacy_agent: "Agent · legacy/未知来源",
    }[kind] || "";
  }

  function renderEvidenceTags(sequences, evidenceMessages) {
    if (!sequences || !sequences.length) {
      return '<span class="text-muted">无 evidence sequence</span>';
    }
    var availability = {};
    (evidenceMessages || []).forEach(function (item) {
      availability[String(item.sequence)] = !!item.available;
    });
    var all = sequences.join(",");
    return sequences.map(function (seq) {
      var available = availability[String(seq)] !== false;
      return (
        '<button type="button" class="evidence-seq' +
        (available ? "" : " missing") +
        '" onclick="window.highlightEvidenceSequences(\'' +
        window.escapeHtml(all) +
        "', " +
        Number(seq) +
        ')" title="' +
        (available ? "查看证据消息" : "原始消息不可用") +
        '">#' +
        window.escapeHtml(String(seq)) +
        "</button>"
      );
    }).join("");
  }

  function reviewDecisionText(review) {
    var decision = String(review.llm_decision || review.review_decision || "").toUpperCase();
    if (decision === "PASS") return "PASS";
    if (decision === "INTERVENE") return "INTERVENE";
    if (review.failure_reason) return "FAILED";
    return decision || review.review_status || "reviewed";
  }

  // ============================================================
  // Initialization
  // ============================================================

 window.initAgentAudit = function () {
   loadSessions();
   loadGroups();
   loadTasks();
   loadStrategies();
   setupModalCloseHandlers();
   // Auto-load when session/group changes
   var ids = [
     "audit-session-select",
     "audit-group-select",
     "audit-task-select",
     "audit-state-filter",
     "audit-intervention-filter",
     "audit-review-filter",
     "audit-agent-filter",
   ];
    for (var i = 0; i < ids.length; i++) {
      var el = document.getElementById(ids[i]);
      if (el) el.addEventListener("change", function () {
        if (this.id === "audit-session-select" || this.id === "audit-group-select") {
          window.loadAuditTimeline();
        } else if (state.auditData) {
          renderTimeline(state.auditData);
        }
      });
    }
  };

  function setupModalCloseHandlers() {
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        closeCorrectionModal();
        closeUnblindModal();
      }
    });
    // Close modals on overlay click
    var correctionModal = document.getElementById("correction-modal");
    if (correctionModal) correctionModal.addEventListener("click", function (e) {
      if (e.target === this) closeCorrectionModal();
    });
    var unblindModal = document.getElementById("unblind-modal");
    if (unblindModal) unblindModal.addEventListener("click", function (e) {
      if (e.target === this) closeUnblindModal();
    });
  }
  // ============================================================
  // Load strategies for enhanced display
  // ============================================================

  async function loadStrategies() {
    try {
      var data = await window.fetchJSON("/api/teacher/strategies");
      state.strategies = data.strategies || [];
    } catch (e) {
      // Non-critical - page continues without strategy metadata
      console.warn("loadStrategies failed:", e);
      state.strategies = [];
    }
  }

  function getStrategyInfo(strategyId) {
    if (!strategyId || !state.strategies || !state.strategies.length) return null;
    for (var i = 0; i < state.strategies.length; i++) {
      if (state.strategies[i].id === strategyId) return state.strategies[i];
    }
    return null;
  }

  function formatStrategyLabel(iv) {
    // Priority: strategy metadata > strategy_type+sub_category > title > fallback
    var info = getStrategyInfo(iv.strategy_id);
    if (info) {
      var label = info.display_name || info.goal || "";
      if (info.strategy_type && info.sub_category && info.sub_category !== "unknown") {
        label = label + " (" + info.strategy_type + "/" + info.sub_category + ")";
      }
      return label;
    }
    if (iv.strategy_type) {
      var extra = iv.sub_category && iv.sub_category !== "unknown" ? "/" + iv.sub_category : "";
      return iv.strategy_type + extra;
    }
    return iv.title || "干预 #" + (iv.intervention_index || iv.id);
  }

  // ============================================================
  // Load filter options
  // ============================================================

  async function loadSessions() {
    try {
      var data = await window.fetchJSON("/api/teacher/sessions?all=true");
      state.sessions = data.sessions || [];
      var sel = document.getElementById("audit-session-select");
      sel.innerHTML =
        '<option value="">-- 请选择课次 --</option>' +
        state.sessions
          .map(function (s) {
            var label =
              "课次 #" + s.id + " | 第" + (s.session_no || "?") + " 课时" + (s.session_role ? " (" + s.session_role + ")" : "");
            return (
              '<option value="' + s.id + '">' + window.escapeHtml(label) + "</option>"
            );
          })
          .join("");
    } catch (e) {
      window.showError("加载课次列表失败: " + e.message);
    }
  }

  async function loadGroups() {
    try {
      var data = await window.fetchJSON("/api/teacher/groups?all=true");
      state.groups = data.groups || [];
      var sel = document.getElementById("audit-group-select");
      sel.innerHTML =
        '<option value="">-- 请选择小组 --</option>' +
        state.groups
          .filter(function (g) {
            return g.group_id != null;
          })
          .map(function (g) {
            return (
              '<option value="' +
              g.group_id +
              '">' +
              window.escapeHtml(g.group_name || g.group_code || g.name || "小组 #" + g.group_id) +
              "</option>"
            );
          })
          .join("");
      if (sel.options.length > 1) {
        sel.selectedIndex = 1;
      }
    } catch (e) {
      window.showError("加载小组列表失败: " + e.message);
    }
  }

  async function loadTasks() {
    try {
      var data = await window.fetchJSON("/api/teacher/tasks");
      state.tasks = data.tasks || [];
      var sel = document.getElementById("audit-task-select");
      sel.innerHTML =
        '<option value="">-- 全部任务 --</option>' +
        state.tasks
          .map(function (t) {
            return (
              '<option value="' +
              t.id +
              '">' +
              window.escapeHtml(t.title || "任务 #" + t.id) +
              "</option>"
            );
          })
          .join("");
    } catch (e) {
      // Non-critical - silently ignore task load failure
      console.warn("loadTasks failed:", e);
    }
  }

  // ============================================================
  // Reset filters
  // ============================================================

  window.resetFilters = function () {
    document.getElementById("audit-session-select").value = "";
    document.getElementById("audit-group-select").value = "";
    document.getElementById("audit-task-select").value = "";
    document.getElementById("audit-state-filter").value = "";
    document.getElementById("audit-intervention-filter").value = "";
    document.getElementById("audit-review-filter").value = "";
    var agentFilter = document.getElementById("audit-agent-filter");
    if (agentFilter) agentFilter.value = "";
    state.currentGroupId = null;
    state.currentSessionId = null;
    state.auditData = null;
    state.selectedEvent = null;
    state.highlightedEvidenceSequences = [];
    document.getElementById("audit-timeline-area").style.display = "none";
    document.getElementById("audit-empty-state").style.display = "block";
    document.getElementById("audit-traceability").innerHTML = "";
    document.getElementById("audit-filter-info").textContent =
      "当前为默认直接显示视图";
  };

  // ============================================================
  // Load Audit Timeline
  // ============================================================

  window.loadAuditTimeline = async function () {
    var gid = parseInt(document.getElementById("audit-group-select").value);
    var sid = parseInt(document.getElementById("audit-session-select").value);

    if (!gid || !sid) {
      window.showError("请先选择课次和小组");
      return;
    }

    state.currentGroupId = gid;
    state.currentSessionId = sid;

    var loadingEl = document.getElementById("audit-filter-info");
    loadingEl.textContent = "查询中...";

    try {
      var blinded = false;
      var data = await window.fetchJSON(
        "/api/teacher/group/" +
          gid +
          "/agent-audit?session_id=" +
          sid +
          "&blinded=" +
          blinded
      );
      state.auditData = data;
      state.selectedEvent = null;
      renderTimeline(data);
      renderBlindingNotice();
      renderTraceabilityWarnings(data);
      loadingEl.textContent = "已加载 " + getTotalEventCount(data) + " 条审计记录";
    } catch (e) {
      window.showError("查询审计数据失败: " + e.message);
      loadingEl.textContent = "查询失败";
    }
  };

  function getTotalEventCount(data) {
    return (
      (data.strategy_pipeline_runs || []).length +
      (data.assessment_batches || []).length +
      (data.strategy_reviews || []).length +
      (data.detector_outputs || []).length +
      (data.gate_records || []).length +
      (data.interventions || []).length +
      (data.uptake || []).length +
      (data.autonomous_regulation_events || []).length
    );
  }

  function makeEvents(data) {
    var events = [];

    (data.strategy_pipeline_runs || []).forEach(function (p) {
      var preview = formatStateLabel(finalStateCode(p)) +
        " | " + (p.selected_strategy_id || "历史数据未记录") +
        " | " + (p.publish_status || p.final_status || "历史数据未记录");
      events.push({
        type: "strategy_pipeline",
        time: p.published_at || p.stage3_completed_at || p.stage2_completed_at || p.stage1_completed_at || p.created_at,
        label: "三阶段审计",
        preview: preview,
        data: p,
        raw: p,
      });
    });

    (data.assessment_batches || []).forEach(function (b) {
      var batchStatus = b.terminal_status || b.status || "unknown";
      var preview = "batch #" + (b.batch_id || b.id) +
        " | " + batchStatus +
        (b.error_code ? " | " + b.error_code : "");
      events.push({
        type: "assessment_batch",
        time: b.completed_at || b.terminal_at || b.started_at || b.created_at,
        label: "状态评估批次",
        preview: preview,
        data: b,
        raw: b,
      });
    });

    (data.strategy_reviews || []).forEach(function (r) {
      var decision = reviewDecisionText(r);
      var preview = decision + (r.llm_final_state ? " | " + formatStateLabel(r.llm_final_state) : "");
      if (r.evidence_sequences && r.evidence_sequences.length) {
        preview += " | evidence #" + r.evidence_sequences.join(", #");
      }
      events.push({
        type: "strategy_review",
        time: r.review_completed_at || r.review_started_at || r.detected_at,
        label: "策略复核",
        preview: preview,
        data: r,
        raw: r,
      });
    });

    (data.detector_outputs || []).forEach(function (d) {
      events.push({
        type: "detector_output",
        time: d.created_at || d.assessed_at,
        label: "检测器输出",
        preview: "第一阶段粗分类 · " + formatCoarseStateLabel(coarseStateCode(d)),
        data: d,
        raw: d,
      });
    });

    (data.gate_records || []).forEach(function (g) {
      events.push({
        type: "gate_decision",
        time: g.created_at || g.decided_at,
        label: "门控决策",
        preview: g.final_decision || (g.should_intervene ? "通过 → 干预" : "拒绝 → 跳过"),
        data: g,
        raw: g,
      });
    });

    (data.interventions || []).forEach(function (iv) {
      var stateCode = finalStateCode(iv);
      events.push({
        type: "intervention",
        time: iv.created_at || iv.delivered_at,
        label: iv.agent_display_label || "干预事件",
        preview: (stateCode ? formatStateLabel(stateCode) + " | " : "") + formatStrategyLabel(iv),
        data: iv,
        raw: iv,
      });
    });

    (data.uptake || []).forEach(function (u) {
      var utype = u.manual_uptake_type || u.auto_uptake_type || "待定";
      events.push({
        type: "uptake",
        time: u.created_at || u.detected_at,
        label: "采纳记录",
        preview: UPTAKE_LABELS[utype] || utype,
        data: u,
        raw: u,
      });
    });

    (data.autonomous_regulation_events || []).forEach(function (e) {
      events.push({
        type: "autonomous_regulation",
        time: e.created_at || e.detected_at,
        label: "自主调节",
        preview: e.event_type || "调节事件",
        data: e,
        raw: e,
      });
    });

    events.sort(function (a, b) {
      if (!a.time && !b.time) return 0;
      if (!a.time) return -1;
      if (!b.time) return 1;
      return a.time < b.time ? -1 : a.time > b.time ? 1 : 0;
    });
    return events;
  }

  function eventMatchesFilters(ev) {
    var stateFilter = document.getElementById("audit-state-filter");
    var interventionFilter = document.getElementById("audit-intervention-filter");
    var reviewFilter = document.getElementById("audit-review-filter");
    var agentFilter = document.getElementById("audit-agent-filter");

    var wantedState = stateFilter ? stateFilter.value : "";
    if (wantedState) {
      var stateCode = ev.type === "strategy_pipeline"
        ? finalStateCode(ev.raw)
        : ev.type === "assessment_batch"
        ? normalizeStateCode(ev.raw.assessment_status)
        : ev.type === "strategy_review"
        ? normalizeStateCode(ev.raw.final_sub_state_code || "unclassified")
        : ev.type === "detector_output"
        ? "unclassified"
        : finalStateCode(ev.raw);
      if (stateCode !== wantedState) return false;
    }

    var wantedAgent = agentFilter ? agentFilter.value : "";
    if (wantedAgent && normalizeAgentType(ev.raw) !== wantedAgent) return false;

    var wantedIntervention = interventionFilter ? interventionFilter.value : "";
    var isPipelinePublished = ev.type === "strategy_pipeline" && String(ev.raw.publish_status || "").toUpperCase() === "PUBLISHED";
    if (wantedIntervention === "yes" && ev.type !== "intervention" && !isPipelinePublished) return false;
    if (wantedIntervention === "no" && (ev.type === "intervention" || isPipelinePublished)) return false;

    var wantedReview = reviewFilter ? reviewFilter.value : "";
    if (wantedReview === "yes" && ev.type !== "strategy_review" && ev.type !== "strategy_pipeline") return false;
    if (wantedReview === "no" && (ev.type === "strategy_review" || ev.type === "strategy_pipeline")) return false;

    return true;
  }

  // ============================================================
  // Render Blinding Notice
  // ============================================================

  function renderBlindingNotice() {
    var notice = document.getElementById("audit-blinding-notice");
    notice.className = "blind-banner unblinded";
    notice.innerHTML =
      '<span class="blind-text">当前为直接显示视图：审计数据默认可见。</span>';
  }

  window.reblind = function () {
    renderBlindingNotice();
    if (state.currentGroupId && state.currentSessionId) {
      window.loadAuditTimeline();
    }
  };

  // ============================================================
  // Render Traceability Warnings
  // ============================================================

  function renderTraceabilityWarnings(data) {
    var el = document.getElementById("audit-traceability");
    var warnings = data.traceability_warnings || [];
    if (warnings.length) {
      el.innerHTML =
        '<section class="card" style="border-color:var(--warning-border);margin-bottom:16px">' +
        '<div class="card-hd" style="background:var(--warning-soft)"><h2 style="font-size:13px;color:var(--warning-text)">数据追溯提示</h2></div>' +
        '<div class="card-bd" style="padding:12px 16px">' +
        warnings
          .map(function (w) {
            return '<div style="font-size:12px;color:var(--text-secondary);margin-bottom:4px">&bull; ' + window.escapeHtml(w) + "</div>";
          })
          .join("") +
        "</div></section>";
    } else {
      el.innerHTML = "";
    }
  }

  function renderStats(data) {
    var el = document.getElementById("audit-stats");
    if (!el) return;
    var s = data.stats || {};
    var items = [
      ["三阶段运行", s.three_stage_pipeline_count || 0],
      ["三阶段已发布", s.three_stage_published_count || 0],
      ["情绪智能体消息", s.emotion_agent_message_count || 0],
      ["策略自动介入", s.strategy_auto_intervention_count || 0],
      ["学生求助回复", s.student_help_reply_count || 0],
      ["LLM PASS 复核", s.llm_pass_review_count || 0],
      ["LLM 失败复核", s.llm_failed_review_count || 0],
      ["Agent 消息总数", s.agent_message_total || 0],
      ["实际介入数", s.actual_intervention_count || 0],
    ];
    el.innerHTML =
      '<div class="stats-grid">' +
      items.map(function (item) {
        return (
          '<div class="stat-card"><span class="stat-label">' +
          window.escapeHtml(item[0]) +
          '</span><strong>' +
          window.escapeHtml(String(item[1])) +
          "</strong></div>"
        );
      }).join("") +
      "</div>" +
      '<div class="stats-note">' +
      window.escapeHtml(s.scope_note || "实际介入数不包含 PASS 或失败复核。") +
      "</div>";
  }

  function activeEvidenceSet() {
    var set = {};
    (state.highlightedEvidenceSequences || []).forEach(function (seq) {
      set[String(seq)] = true;
    });
    return set;
  }

  function renderMessageFlow(data) {
    var el = document.getElementById("audit-message-flow");
    if (!el) return;
    var messages = data.message_timeline || [];
    var agentFilter = document.getElementById("audit-agent-filter");
    var wantedAgent = agentFilter ? agentFilter.value : "";
    var highlighted = activeEvidenceSet();
    var visible = messages.filter(function (m) {
      return !wantedAgent || normalizeAgentType(m) === wantedAgent || m.role !== "agent";
    });
    if (!visible.length) {
      el.innerHTML = '<div class="empty-state">当前课次暂无消息</div>';
      return;
    }
    var clearButton = state.highlightedEvidenceSequences.length
      ? '<button class="btn small secondary" onclick="window.clearEvidenceHighlight()">取消高亮</button>'
      : "";
    el.innerHTML =
      '<div class="message-flow-actions">' +
      clearButton +
      "</div>" +
      visible.map(function (m) {
        var seq = m.sequence || m.id;
        var isEvidence = highlighted[String(seq)];
        var isAgent = m.role === "agent";
        return (
          '<article class="audit-message-card' +
          (isAgent ? " agent" : "") +
          (isEvidence ? " evidence-hit" : "") +
          '" data-sequence="' +
          window.escapeHtml(String(seq)) +
          '">' +
          '<div class="audit-message-meta"><span>#' +
          window.escapeHtml(String(seq)) +
          "</span><span>" +
          window.escapeHtml(window.formatDt(m.created_at) || "--") +
          "</span><span>" +
          window.escapeHtml(m.display_name || "-") +
          "</span>" +
          (isAgent ? '<span class="badge low">' + window.escapeHtml(m.agent_display_label || agentKindLabel(m.agent_message_kind) || "Agent · 旧记录未分类") + "</span>" : "") +
          (isEvidence ? '<span class="badge mid">策略判断证据</span>' : "") +
          "</div>" +
          '<div class="audit-message-content">' +
          window.escapeHtml(m.content || "") +
          "</div></article>"
        );
      }).join("");
  }

  // ============================================================
  // Render Timeline
  // ============================================================

  function renderTimeline(data) {
    document.getElementById("audit-empty-state").style.display = "none";
    document.getElementById("audit-timeline-area").style.display = "block";

    renderStats(data);
    renderMessageFlow(data);

    var events = makeEvents(data).filter(eventMatchesFilters);

    var countEl = document.getElementById("audit-event-count");
    countEl.textContent = events.length + "/" + getTotalEventCount(data) + " 条";

    var timelineEl = document.getElementById("audit-timeline");

    if (!events.length) {
      timelineEl.innerHTML =
        '<div class="empty-state"><p>暂无审计记录</p></div>';
      return;
    }

    timelineEl.innerHTML = events
      .map(function (ev, idx) {
        var dotClass = EVENT_DOT_CLASSES[ev.type] || "";
        var timeStr = window.formatDt(ev.time) || "--";
        var selClass = state.selectedEvent === idx ? " selected" : "";
        return (
          '<div class="timeline-item" data-event-idx="' +
          idx +
          '" onclick="window.selectEvent(' +
          idx +
          ')">' +
          '<div class="timeline-dot ' +
          dotClass +
          '"></div>' +
          '<div class="timeline-card' +
          selClass +
          '">' +
          '<div class="timeline-time">' +
          window.escapeHtml(timeStr) +
          "</div>" +
          '<div class="timeline-type" style="color:var(--' +
          (dotClass === "detector"
            ? "primary"
            : dotClass === "gate"
            ? "warning-text"
            : dotClass === "intervention"
            ? "danger-text"
            : dotClass === "uptake"
            ? "success-text"
            : dotClass === "regulation"
            ? "text-secondary"
            : "text-muted") +
          ')">' +
          window.escapeHtml(ev.label) +
          "</div>" +
          '<div class="timeline-preview">' +
          window.escapeHtml(String(ev.preview || "").substring(0, 120)) +
          "</div>" +
          "</div>" +
          "</div>"
        );
      })
      .join("");

    // Auto-select first event
    if (events.length > 0) {
      window.selectEvent(0);
    }
  }

  // ============================================================
  // Select event - show detail
  // ============================================================

  window.selectEvent = function (idx) {
    state.selectedEvent = idx;
    var events = buildEventList();
    if (!events || idx < 0 || idx >= events.length) return;

    var ev = events[idx];
    var detailEl = document.getElementById("audit-detail-content");
    var titleEl = document.getElementById("audit-detail-title");
    titleEl.textContent = EVENT_TYPE_LABELS[ev.type] || "详情";

    // Update selected state in timeline
    document.querySelectorAll(".timeline-item").forEach(function (item) {
      var itemIdx = parseInt(item.getAttribute("data-event-idx"));
      item.querySelector(".timeline-card").classList.toggle("selected", itemIdx === idx);
    });

    switch (ev.type) {
      case "strategy_pipeline":
        renderStrategyPipelineDetail(detailEl, ev.raw);
        break;
      case "assessment_batch":
        renderAssessmentBatchDetail(detailEl, ev.raw);
        break;
      case "strategy_review":
        renderStrategyReviewDetail(detailEl, ev.raw);
        break;
      case "detector_output":
        renderDetectorDetail(detailEl, ev.raw);
        break;
      case "gate_decision":
        renderGateDetail(detailEl, ev.raw);
        break;
      case "intervention":
        renderInterventionDetail(detailEl, ev.raw);
        break;
      case "uptake":
        renderUptakeDetail(detailEl, ev.raw);
        break;
      case "autonomous_regulation":
        renderRegulationDetail(detailEl, ev.raw);
        break;
      default:
        detailEl.innerHTML = '<div class="evidence">暂无详情数据</div>';
    }
  };

  function buildEventList() {
    if (!state.auditData) return [];
    return makeEvents(state.auditData).filter(eventMatchesFilters);
  }

  // ============================================================
  // Detail Renderers
  // ============================================================

  function renderStrategyPipelineDetail(el, p) {
    var fields = [
      { label: "pipeline_run_id", value: p.pipeline_run_id },
      { label: "run_uuid", value: p.run_uuid },
      { label: "触发来源", value: p.trigger_source },
      { label: "触发消息", value: p.trigger_message_id },
      { label: "优先级", value: p.trigger_priority != null ? "P" + p.trigger_priority : null },
      { label: "消息窗口", value: (p.input_start_sequence || "?") + "-" + (p.input_end_sequence || "?") },
      { label: "截止学生序号", value: p.input_cutoff_student_sequence },
      { label: "第一阶段状态", value: p.stage1_status },
      { label: "粗状态", value: p.coarse_state_code },
      { label: "粗判断", value: p.coarse_decision },
      { label: "规则置信度", value: p.coarse_confidence },
      { label: "第二阶段状态", value: p.stage2_status },
      { label: "最终主子状态", value: formatPreciseStateLabel(p.final_sub_state_code) },
      { label: "原始子状态", value: p.raw_sub_state_code },
      { label: "Assignment source", value: p.assignment_source },
      { label: "Assignment status", value: p.assessment_status },
      { label: "状态区间", value: (p.sub_state_start_sequence || "?") + "-" + (p.sub_state_end_sequence || "?") },
      { label: "子状态置信度", value: p.sub_state_confidence },
      { label: "自主调节", value: p.detected_self_regulation },
      { label: "是否介入", value: p.should_intervene },
      { label: "OI 策略", value: p.inhibition_strategy_id },
      { label: "OI/抑制原因", value: p.inhibition_reason },
      { label: "第三阶段状态", value: p.stage3_status },
      { label: "最终策略 ID", value: p.selected_strategy_id },
      { label: "策略名称", value: p.selected_strategy_name },
      { label: "策略类型", value: p.selected_strategy_type },
      { label: "策略库版本", value: p.strategy_library_version },
      { label: "发布状态", value: p.publish_status },
      { label: "发布消息", value: p.published_message_id },
      { label: "发布时间", value: window.formatDt(p.published_at) },
      { label: "Pipeline 最终状态", value: p.final_status },
      { label: "跳过原因", value: p.skip_reason },
      { label: "失败代码", value: p.failure_code },
      { label: "观察结果", value: p.observation_summary },
    ];
    var html = renderFieldGrid(fields);
    var evidenceSequences = p.evidence_sequences || p.evidence_message_ids || [];
    if (evidenceSequences.length) {
      html +=
        '<div style="margin-top:12px"><span class="detail-label" style="font-size:11px;color:var(--text-muted);font-weight:700">evidence_message_ids</span>' +
        '<div class="evidence-seq-row">' +
        renderEvidenceTags(evidenceSequences, p.evidence_messages || []) +
        "</div></div>";
    }
    var missing = (p.evidence_messages || []).filter(function (item) { return !item.available; });
    if (missing.length) {
      html +=
        '<div class="notice-card notice-warning" style="margin-top:12px">证据消息不可用或为旧数据：#' +
        missing.map(function (item) { return window.escapeHtml(String(item.ref || item.sequence)); }).join("、#") +
        "</div>";
    }
    html += renderTags("辅助标签", p.secondary_sub_state_tags || []);
    html += renderJsonBlock("规则分数", p.coarse_rule_scores || {});
    html += renderJsonBlock("量化特征", p.coarse_quantitative_features || {});
    html += renderJsonBlock("候选策略", p.strategy_candidate_ids || []);
    html += renderJsonBlock("辅助策略", p.supporting_strategy_ids || []);
    html += renderJsonBlock("文本校验", p.text_validation_result || {});
    html += renderJsonBlock("全部状态段", p.all_state_segments || []);
    html += renderJsonBlock("观察详情", p.observation_details || {});

    [
      ["策略选择理由", p.strategy_selection_reason],
      ["原始话术", p.generated_intervention_text],
      ["校验后话术", p.validated_intervention_text],
      ["已发布消息文本", p.published_message_content],
      ["失败详情", p.failure_detail],
    ].forEach(function (item) {
      if (!item[1]) return;
      html +=
        '<div style="margin-top:12px"><span class="detail-label" style="font-size:11px;color:var(--text-muted);font-weight:700">' +
        window.escapeHtml(item[0]) +
        '</span><div class="evidence" style="margin-top:4px;white-space:pre-wrap">' +
        window.escapeHtml(String(item[1])) +
        "</div></div>";
    });
    el.innerHTML = html || '<div class="evidence">暂无三阶段审计数据</div>';
  }

  function renderAssessmentBatchDetail(el, b) {
    var fields = [
      { label: "Batch ID", value: b.batch_id || b.id },
      { label: "讨论 ID", value: b.discussion_id },
      { label: "候选消息区间", value: (b.candidate_start_sequence || "?") + "-" + (b.candidate_end_sequence || "?") },
      { label: "上下文区间", value: (b.context_start_sequence || "?") + "-" + (b.context_end_sequence || "?") },
      { label: "批次状态", value: b.status },
      { label: "终止状态", value: b.terminal_status },
      { label: "Assignment status", value: b.assessment_status },
      { label: "Assignment source", value: b.assignment_source },
      { label: "错误代码", value: b.error_code },
      { label: "错误详情", value: b.error_detail },
      { label: "Fallback action", value: b.fallback_action },
      { label: "Fallback segments", value: b.fallback_segment_count },
      { label: "尝试次数", value: (b.attempt_count || 0) + "/" + (b.max_attempts || 0) },
      { label: "模型", value: b.model },
      { label: "Prompt 版本", value: b.prompt_version },
      { label: "原始响应长度", value: b.raw_response_length },
      { label: "开始时间", value: window.formatDt(b.started_at) },
      { label: "完成时间", value: window.formatDt(b.completed_at || b.terminal_at) },
    ];
    var html = renderFieldGrid(fields);
    html += renderJsonBlock("LLM 错误", b.llm_error || {});
    el.innerHTML = html || '<div class="evidence">暂无状态批次数据</div>';
  }

  function renderStrategyReviewDetail(el, r) {
    var fields = [
      { label: "规则候选状态", value: r.rule_candidate_state ? formatStateLabel(r.rule_candidate_state) : null },
      { label: "LLM decision", value: reviewDecisionText(r) },
      { label: "LLM final_state", value: r.llm_final_state ? formatStateLabel(r.llm_final_state) : null },
      { label: "confidence", value: r.confidence },
      { label: "reason", value: r.reason },
      { label: "context_from_sequence", value: r.context_from_sequence },
      { label: "context_to_sequence", value: r.context_to_sequence },
      { label: "strategy_id", value: r.strategy_id },
      { label: "prompt_version", value: r.prompt_version },
      { label: "detected", value: window.formatDt(r.detected_at) },
      { label: "reviewed", value: window.formatDt(r.review_completed_at || r.review_started_at) },
      { label: "published", value: window.formatDt(r.published_at) },
      { label: "failure/skip", value: r.failure_reason || r.skip_reason },
    ];
    var html = renderFieldGrid(fields);
    html +=
      '<div style="margin-top:12px"><span class="detail-label" style="font-size:11px;color:var(--text-muted);font-weight:700">evidence_sequences</span>' +
      '<div class="evidence-seq-row">' +
      renderEvidenceTags(r.evidence_sequences || [], r.evidence_messages || []) +
      "</div></div>";

    var missing = (r.evidence_messages || []).filter(function (item) { return !item.available; });
    if (missing.length) {
      html +=
        '<div class="notice-card notice-warning" style="margin-top:12px">原始消息不可用：#' +
        missing.map(function (item) { return window.escapeHtml(String(item.sequence)); }).join("、#") +
        "</div>";
    }
    html += renderJsonBlock("input_message_sequences", r.input_message_sequences || []);
    if (r.generated_message) {
      html +=
        '<div style="margin-top:12px"><span class="detail-label" style="font-size:11px;color:var(--text-muted);font-weight:700">generated_message</span>' +
        '<div class="evidence" style="margin-top:4px;white-space:pre-wrap">' +
        window.escapeHtml(r.generated_message) +
        "</div></div>";
    }
    el.innerHTML = html || '<div class="evidence">暂无策略复核数据</div>';
  }

  function renderDetectorDetail(el, d) {
    var stateInfo = d.audit_state || {};
    var fields = [
      { label: "阶段语义", value: "第一阶段粗分类（非最终主子状态）" },
      { label: "第一阶段粗状态", value: formatCoarseStateLabel(coarseStateCode(d)) },
      { label: "原始状态", value: d.raw_state_code },
      { label: "原始融合状态", value: d.raw_fused_state_code },
      { label: "Legacy 状态", value: d.legacy_state_code || stateInfo.legacy_state_code },
      { label: "归一化原因", value: d.normalization_reason || stateInfo.normalization_reason },
      { label: "规则状态", value: stateInfo.rule_state_code },
      { label: "LLM 状态", value: stateInfo.llm_state_code },
      { label: "融合来源", value: stateInfo.decision_source },
      { label: "调节差距", value: d.regulation_gap },
      { label: "SSRL 过程", value: d.ssrl_process },
      { label: "Valence", value: d.valence },
      { label: "Activation", value: d.activation },
      { label: "置信度", value: d.confidence != null ? d.confidence : d.state_score },
      { label: "持续时间", value: d.duration != null ? d.duration + "s" : null },
      { label: "自我修复", value: d.self_repair != null ? (d.self_repair ? "是" : "否") : null },
      { label: "任务阶段", value: d.task_phase },
      { label: "检测器版本", value: d.detector_version },
      { label: "降级使用", value: d.fallback_used != null ? (d.fallback_used ? "是" : "否") : null },
      { label: "风险等级", value: d.risk_level != null ? "Level " + d.risk_level : null },
      { label: "时间", value: window.formatDt(d.created_at || d.assessed_at) },
    ];

    var html = renderFieldGrid(fields);
    html += renderTags("Evidence Tags", d.evidence_tags || stateInfo.evidence_tags || []);
    html += renderScoreTable(d.candidate_scores || stateInfo.candidate_scores || {});
    el.innerHTML = html || '<div class="evidence">暂无详情数据</div>';
  }

  function renderGateDetail(el, g) {
    // Show 6 gates
    var gateFields = [
      { label: "置信度门", key: "confidence_gate" },
      { label: "持续时间门", key: "duration_gate" },
      { label: "冷却时间门", key: "cooldown_gate" },
      { label: "预算门", key: "budget_gate" },
      { label: "安全门", key: "safety_gate" },
      { label: "条件门", key: "condition_gate" },
    ];

    var html = '<div class="detail-grid">';

    gateFields.forEach(function (gf) {
      var val = g[gf.key];
      var displayVal = val != null ? (String(val) === "true" ? "通过" : String(val) === "false" ? "拒绝" : String(val)) : "--";
      html +=
        '<div class="detail-field"><span class="detail-label">' +
        window.escapeHtml(gf.label) +
        '</span><span class="detail-value">' +
        window.escapeHtml(displayVal) +
        "</span></div>";
    });

    // Additional gate fields
    var gating = g.gating_result || {};
    var extraFields = [
      { label: "最终决策", value: gating.decision || (g.final_decision != null ? (g.final_decision ? "干预" : "跳过") : g.should_intervene != null ? (g.should_intervene ? "干预" : "跳过") : null) },
      { label: "决策理由", value: gating.reason || g.reason || g.decision_reason },
      { label: "抑制原因", value: gating.suppressed_reason || g.suppressed_reason },
      { label: "目标", value: gating.target || g.target },
      { label: "优先级", value: (gating.priority || g.priority) != null ? "P" + (gating.priority || g.priority) : null },
      { label: "策略类别", value: gating.strategy_category || g.strategy_category },
      { label: "选中策略", value: gating.selected_strategy_id || g.selected_strategy_id },
      { label: "时间", value: window.formatDt(g.created_at || g.decided_at) },
    ];

    extraFields
      .filter(function (f) { return f.value != null && f.value !== ""; })
      .forEach(function (f) {
        html +=
          '<div class="detail-field"><span class="detail-label">' +
          window.escapeHtml(f.label) +
          '</span><span class="detail-value">' +
          window.escapeHtml(String(f.value)) +
          "</span></div>";
      });

    html += "</div>";
    html += renderJsonBlock("Cooldown Check", g.cooldown_check || gating.cooldown_check);
    el.innerHTML = html;
  }

  function renderInterventionDetail(el, iv) {
    var trace = iv.intervention_trace || {};
    var gating = trace.gating_result || {};
    var fields = [
      { label: "Agent 类型", value: iv.agent_display_label },
      { label: "最终主子状态", value: iv.final_sub_state_code ? formatStateLabel(iv.final_sub_state_code) : "未记录" },
      { label: "第一阶段粗状态", value: iv.coarse_state_code ? formatCoarseStateLabel(iv.coarse_state_code) : null },
      { label: "触发来源", value: iv.trigger_source || trace.trigger_source },
      { label: "Run 状态", value: trace.status },
      { label: "门控动作", value: gating.action },
      { label: "门控原因", value: gating.reason },
      { label: "干预序号", value: iv.intervention_index != null ? "#" + iv.intervention_index : null },
      { label: "策略 ID", value: iv.strategy_id },
      { label: "策略类型", value: iv.strategy_type },
      { label: "子类别", value: iv.sub_category },
      { label: "策略名称", value: iv.title },
      { label: "context_from_sequence", value: iv.context_from_sequence || trace.context_from_sequence },
      { label: "context_to_sequence", value: iv.context_to_sequence || trace.context_to_sequence },
      { label: "SSRL 目标", value: iv.ssrl_target },
      { label: "干预级别", value: iv.intervention_level },
      { label: "提示版本", value: iv.prompt_version },
      { label: "策略类型+子类", value: (iv.strategy_type ? iv.strategy_type + (iv.sub_category && iv.sub_category !== 'unknown' ? '/' + iv.sub_category : '') : '') },
      { label: "已用预算", value: iv.budget_used != null ? iv.budget_used + "/" + (iv.max_budget || "?") : null },
      { label: "最大预算", value: iv.max_budget },
      { label: "投递时间", value: window.formatDt(iv.delivered_at || iv.created_at) },
    ];

    var html = renderFieldGrid(fields);
    var evidenceSequences = iv.evidence_sequences || trace.evidence_sequences || [];
    if (evidenceSequences.length) {
      html +=
        '<div style="margin-top:12px"><span class="detail-label" style="font-size:11px;color:var(--text-muted);font-weight:700">evidence_sequences</span>' +
        '<div class="evidence-seq-row">' +
        renderEvidenceTags(evidenceSequences, null) +
        "</div></div>";
    }
    html += renderJsonBlock("Cooldown Check", iv.cooldown_check || trace.cooldown_check || gating.cooldown_check);
    html += renderJsonBlock("Intervention Gating", gating);

    if (iv.message) {
      html +=
        '<div style="margin-top:12px"><span class="detail-label" style="font-size:11px;color:var(--text-muted);font-weight:700">Agent 原文</span>' +
        '<div class="evidence" style="margin-top:4px;white-space:pre-wrap">' +
        window.escapeHtml(iv.message) +
        "</div></div>";
    } else if (iv.message) {
      html +=
        '<div style="margin-top:12px"><span class="detail-label" style="font-size:11px;color:var(--text-muted)">Agent 原文</span></div>';
    }

    if (iv.condition) {
      html +=
        '<div style="margin-top:8px"><span class="detail-label" style="font-size:11px;color:var(--text-muted);font-weight:700">Condition</span>' +
        '<div style="font-size:13px;margin-top:2px">' +
        window.escapeHtml(iv.condition) +
        "</div></div>";
    }

    el.innerHTML = html;
  }

  function renderUptakeDetail(el, u) {
    var utype = u.manual_uptake_type || u.auto_uptake_type || "待定";
    var uptypeLabel = UPTAKE_LABELS[utype] || utype;

    var fields = [
      { label: "采纳类型", value: utype + " (" + uptypeLabel + ")" },
      { label: "证据", value: u.evidence },
      { label: "检测时间", value: window.formatDt(u.detected_at || u.created_at) },
      { label: "来源", value: u.corrected_by ? "人工校正" : "自动检测" },
      { label: "校正者", value: u.corrected_by ? "教师 #" + u.corrected_by : null },
      { label: "校正理由", value: u.reason || u.corrected_reason },
      { label: "SSRL 行为", value: u.target_ssrl_behavior },
      { label: "行为发生", value: u.target_behavior_occurred != null ? (u.target_behavior_occurred ? "是" : "否") : null },
    ].filter(function (f) { return f.value != null && f.value !== ""; });

    var html =
      '<div class="detail-grid">' +
      fields
        .map(function (f) {
          return (
            '<div class="detail-field"><span class="detail-label">' +
            window.escapeHtml(f.label) +
            '</span><span class="detail-value">' +
            window.escapeHtml(String(f.value)) +
            "</span></div>"
          );
        })
        .join("") +
      "</div>";

    // Correction button
    var interventionId = u.intervention_id;
    if (interventionId) {
      html +=
        '<div style="margin-top:12px">' +
        '<button class="btn small secondary" onclick="window.openManualCorrectionModal(' +
        u.id +
        ", " +
        interventionId +
        ', \'' +
        window.escapeHtml(utype) +
        "')\">人工校正采纳类型</button>" +
        "</div>";
    }

    el.innerHTML = html;
  }

  function renderRegulationDetail(el, e) {
    var fields = [
      { label: "事件类型", value: e.event_type },
      { label: "置信度", value: e.confidence != null ? e.confidence : null },
      { label: "检测来源", value: e.detected_by },
      { label: "备注", value: e.note },
      { label: "时间", value: window.formatDt(e.created_at || e.detected_at) },
    ].filter(function (f) { return f.value != null && f.value !== ""; });

    var html =
      '<div class="detail-grid">' +
      fields
        .map(function (f) {
          return (
            '<div class="detail-field"><span class="detail-label">' +
            window.escapeHtml(f.label) +
            '</span><span class="detail-value">' +
            window.escapeHtml(String(f.value)) +
            "</span></div>"
          );
        })
        .join("") +
      "</div>";
    el.innerHTML = html;
  }

  // ============================================================
  // Manual Correction Modal
  // ============================================================

  window.openManualCorrectionModal = function (uptakeId, interventionId, currentType) {
    state.correctionTargetId = uptakeId;
    state.correctionInterventionId = interventionId;
    document.getElementById("correction-reason").value = "";
    document.getElementById("correction-status").textContent = "";

    // Pre-select the current type
    var sel = document.getElementById("correction-type-select");
    sel.value = UPTAKE_TYPES.indexOf(currentType) >= 0 ? currentType : "acknowledged";

    document.getElementById("correction-modal").classList.add("visible");
    document.getElementById("correction-reason").focus();
  };

  window.closeCorrectionModal = function () {
    document.getElementById("correction-modal").classList.remove("visible");
    state.correctionTargetId = null;
    state.correctionInterventionId = null;
  };

  window.submitManualCorrection = async function () {
    var type = document.getElementById("correction-type-select").value;
    var reason = document.getElementById("correction-reason").value.trim();
    var statusEl = document.getElementById("correction-status");

    if (!reason) {
      statusEl.textContent = "错误：必须填写校正理由";
      statusEl.style.color = "var(--danger-text)";
      return;
    }

    if (!UPTAKE_TYPES.includes(type)) {
      statusEl.textContent = "错误：" + window.escapeHtml(type) + " 不是合法的采纳类型";
      statusEl.style.color = "var(--danger-text)";
      return;
    }

    var interventionId = state.correctionInterventionId;
    if (!interventionId) {
      statusEl.textContent = "错误：未找到关联的干预记录";
      statusEl.style.color = "var(--danger-text)";
      return;
    }

    statusEl.textContent = "保存中...";
    statusEl.style.color = "var(--text-muted)";

    try {
      await window.fetchJSON(
        "/api/teacher/intervention/" + interventionId + "/manual-uptake",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            manual_uptake_type: type,
            reason: reason,
          }),
        }
      );
      statusEl.textContent = "校正已保存！";
      statusEl.style.color = "var(--success-text)";
      window.closeCorrectionModal();
      // Reload audit data
      window.loadAuditTimeline();
    } catch (e) {
      statusEl.textContent = "保存失败: " + window.escapeHtml(e.message);
      statusEl.style.color = "var(--danger-text)";
    }
  };

  // ============================================================
  // Unblind Modal
  // ============================================================

  window.openUnblindModal = function () {
    return false;
  };

  window.closeUnblindModal = function () {
    var modal = document.getElementById("unblind-modal");
    if (modal) modal.classList.remove("visible");
  };

  window.submitUnblindRequest = async function () {
    return false;
  };

  window.highlightEvidenceSequences = function (sequenceCsv, focusSeq) {
    var sequences = String(sequenceCsv || "")
      .split(",")
      .map(function (item) { return parseInt(item, 10); })
      .filter(function (item) { return !Number.isNaN(item); });
    state.highlightedEvidenceSequences = sequences;
    renderMessageFlow(state.auditData || {});
    var target = document.querySelector(
      '.audit-message-card[data-sequence="' + CSS.escape(String(focusSeq)) + '"]'
    );
    if (target) {
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      target.animate(
        [{ outline: "2px solid var(--primary)" }, { outline: "2px solid transparent" }],
        { duration: 1200 }
      );
    } else {
      window.showError("原始消息不可用：#" + focusSeq);
    }
  };

  window.clearEvidenceHighlight = function () {
    state.highlightedEvidenceSequences = [];
    renderMessageFlow(state.auditData || {});
  };

  // ============================================================
  // Error Display
  // ============================================================

  window.showError = function (msg) {
    var el = document.getElementById("audit-error");
    if (!el) return;
    el.textContent = msg;
    el.style.display = "block";
    setTimeout(function () {
      el.style.display = "none";
    }, 6000);
  };

  // Expose internal functions for testing / debugging
  window.__auditState = state;
})();
