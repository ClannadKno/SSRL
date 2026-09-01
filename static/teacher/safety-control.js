// SSRL-ESP Safety Control (T8) - Teacher UI
// Provides: initSafetyControl, loadSafetyStatus, renderGroupSafetyList,
//   openSafetyModal, submitPauseAgent, submitResumeAgent,
//   submitPauseSession, submitResumeSession, requireReason,
//   showConfirm, showError

(function() {
  "use strict";

  var SAFETY_API = "/api/teacher/safety/overview";
  var _sessionId = null;
  var _requestInFlight = false;

  // ============================================================
  // Exports
  // ============================================================

  window.initSafetyControl = initSafetyControl;
  window.openSafetyControl = openSafetyControl;
  window.loadSafetyStatus = loadSafetyStatus;

  // ============================================================
  // Initialization
  // ============================================================

  function initSafetyControl(sessionId) {
    _sessionId = sessionId || null;
    // Auto-detect session ID from the active session if not provided
    if (!_sessionId) {
      fetch("/api/teacher/status/current", { credentials: "same-origin" })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          if (data && data.current_session && data.current_session.id) {
            _sessionId = data.current_session.id;
          }
        })
        .catch(function() {});
    }
  }

  // ============================================================
  // Open Safety Control Modal
  // ============================================================

  function openSafetyControl(modalId) {
    modalId = modalId || "safetyControlModal";
    // Ensure sessionId is resolved
    if (!_sessionId) {
      fetch("/api/teacher/status/current", { credentials: "same-origin" })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          if (data && data.current_session && data.current_session.id) {
            _sessionId = data.current_session.id;
          }
          _doOpenModal(modalId);
        })
        .catch(function() {
          _doOpenModal(modalId);
        });
    } else {
      _doOpenModal(modalId);
    }
  }

  function _doOpenModal(modalId) {
    var modal = document.getElementById(modalId);
    if (!modal) {
      showError("安全控制弹窗未找到，请刷新页面重试。");
      return;
    }
    modal.style.display = "flex";
    loadSafetyStatus();
  }

  // ============================================================
  // Load Safety Status
  // ============================================================

  function loadSafetyStatus() {
    var contentEl = document.getElementById("safetyGroupList");
    if (!contentEl) return;
    contentEl.innerHTML = "<div class=\"evidence\">加载中...</div>";

    // If no session, try to get active session
    if (!_sessionId) {
      fetch("/api/teacher/status/current", { credentials: "same-origin" })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          if (data && data.current_session && data.current_session.id) {
            _sessionId = data.current_session.id;
          }
          _doFetchSafety(contentEl);
        })
        .catch(function(err) {
          contentEl.innerHTML = "<div class=\"evidence\" style=\"color:var(--danger-text)\">无法获取当前课次信息: " + escapeHtml(err.message) + "</div>";
        });
    } else {
      _doFetchSafety(contentEl);
    }
  }

  function _doFetchSafety(contentEl) {
    var url = SAFETY_API;
    if (_sessionId) {
      url += "?session_id=" + _sessionId;
    }

    fetch(url, { credentials: "same-origin" })
      .then(function(r) {
        if (!r.ok) throw new Error("API 请求失败: " + r.status);
        return r.json();
      })
      .then(function(data) {
        renderGroupSafetyList(data.groups || []);
      })
      .catch(function(err) {
        contentEl.innerHTML = "<div class=\"evidence\" style=\"color:var(--danger-text)\">加载安全状态失败: " + escapeHtml(err.message) + "</div>";
      });
  }

  // ============================================================
  // Render Group Safety List
  // ============================================================

  function renderGroupSafetyList(groups) {
    var el = document.getElementById("safetyGroupList");
    if (!el) return;

    if (!groups.length) {
      el.innerHTML = "<div class=\"evidence\">暂无小组数据。</div>";
      return;
    }

    el.innerHTML = groups.map(function(g) {
      return _renderGroupRow(g);
    }).join("");
  }

  function _renderGroupRow(g) {
    var agentStatus = g.agent_paused
      ? "<span class=\"badge high\">Agent 已暂停</span>"
      : "<span class=\"badge low\">Agent 正常</span>";

    var sessionStatus = g.session_paused
      ? "<span class=\"badge high\">会话已暂停</span>"
      : "<span class=\"badge low\">会话正常</span>";

    // Determine which S role - disable agent buttons if not allowed
    var agentDisabled = false;
    var agentDisabledReason = "";

    // Buttons - show only the relevant ones
    var agentBtn = "";
    if (g.agent_paused) {
      agentBtn = "<button class=\"btn small\" onclick=\"submitResumeAgent(" + g.group_id + "," + (_sessionId || "null") + ")\" data-action-id=\"resume-agent-" + g.group_id + "\">恢复 Agent</button>";
    } else {
      agentBtn = "<button class=\"btn small danger\" onclick=\"submitPauseAgent(" + g.group_id + "," + (_sessionId || "null") + ")\" data-action-id=\"pause-agent-" + g.group_id + "\">暂停 Agent</button>";
    }

    var sessionBtn = "";
    if (g.session_paused) {
      sessionBtn = "<button class=\"btn small\" onclick=\"submitResumeSession(" + g.group_id + "," + (_sessionId || "null") + ")\" data-action-id=\"resume-session-" + g.group_id + "\">恢复会话</button>";
    } else {
      sessionBtn = "<button class=\"btn small danger\" onclick=\"submitPauseSession(" + g.group_id + "," + (_sessionId || "null") + ")\" data-action-id=\"pause-session-" + g.group_id + "\">暂停会话</button>";
    }

    // Latest signal info
    var signalHtml = "";
    if (g.latest_signal) {
      signalHtml = "<span class=\"text-xs\" style=\"color:var(--text-muted)\">" + escapeHtml(g.latest_signal.signal_type) + " (" + escapeHtml(g.latest_signal.severity || "") + ")</span>";
    } else {
      signalHtml = "<span class=\"text-xs\" style=\"color:var(--text-muted)\">--</span>";
    }

    // Latest action at
    var actionTimeHtml = g.latest_action_at
      ? "<span class=\"text-xs\" style=\"color:var(--text-muted)\">" + formatDt(g.latest_action_at) + "</span>"
      : "<span class=\"text-xs\" style=\"color:var(--text-muted)\">--</span>";

    return "<div class=\"group-card safety-group-row\" data-group-id=\"" + g.group_id + "\">" +
      "<div class=\"group-card-head\">" +
        "<div><div class=\"group-title\">" + escapeHtml(g.group_code) + "</div></div>" +
        "<div style=\"display:flex;gap:6px;flex-wrap:wrap;align-items:center\">" +
          agentStatus + sessionStatus +
        "</div>" +
      "</div>" +
      "<div class=\"safety-group-meta\" style=\"display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--text-muted)\">" +
        "<span><strong>最近信号:</strong> " + signalHtml + "</span>" +
        "<span><strong>最近操作:</strong> " + actionTimeHtml + "</span>" +
      "</div>" +
      "<div class=\"teacher-actions\" style=\"margin-top:8px\">" +
        agentBtn + sessionBtn +
      "</div>" +
    "</div>";
  }

  // ============================================================
  // Reason Dialog
  // ============================================================

  window.requireReason = function(actionName, callback) {
    // Show a modal overlay for reason input
    var backdrop = document.createElement("div");
    backdrop.className = "safety-reason-backdrop";
    backdrop.style.cssText = "position:fixed;inset:0;z-index:2000;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.35);backdrop-filter:blur(2px)";

    var dialog = document.createElement("div");
    dialog.className = "safety-reason-dialog card";
    dialog.style.cssText = "width:min(440px,92vw);max-height:80vh;overflow-y:auto";

    var quickOptions = [
      "高风险内容",
      "教师人工接管",
      "学生主动求助",
      "技术异常",
      "误暂停恢复",
      "其他"
    ];

    dialog.innerHTML = "<div class=\"card-hd\"><h2>操作原因</h2></div>" +
      "<div class=\"card-bd\" style=\"display:grid;gap:12px\">" +
        "<p style=\"margin:0;font-size:13px;color:var(--text-secondary)\">" + escapeHtml(actionName) + "</p>" +
        "<div class=\"safety-reason-options\" style=\"display:flex;gap:6px;flex-wrap:wrap\">" +
          quickOptions.map(function(opt) {
            return "<button class=\"btn small secondary\" type=\"button\" data-reason=\"" + escapeHtml(opt) + "\">" + escapeHtml(opt) + "</button>";
          }).join("") +
        "</div>" +
        "<textarea id=\"safetyReasonInput\" placeholder=\"请输入操作原因（必填）\" style=\"min-height:60px\"></textarea>" +
        "<div style=\"display:flex;gap:8px;justify-content:flex-end\">" +
          "<button class=\"btn small secondary\" id=\"safetyReasonCancel\">取消</button>" +
          "<button class=\"btn small\" id=\"safetyReasonConfirm\">确认</button>" +
        "</div>" +
        "<div id=\"safetyReasonError\" class=\"text-xs\" style=\"color:var(--danger-text);display:none\">请输入操作原因</div>" +
      "</div>";

    backdrop.appendChild(dialog);
    document.body.appendChild(backdrop);

    // Quick option click handler
    dialog.querySelectorAll(".safety-reason-options button").forEach(function(btn) {
      btn.addEventListener("click", function() {
        document.getElementById("safetyReasonInput").value = btn.getAttribute("data-reason");
        document.getElementById("safetyReasonError").style.display = "none";
      });
    });

    // Cancel
    document.getElementById("safetyReasonCancel").addEventListener("click", function() {
      document.body.removeChild(backdrop);
    });

    // Confirm
    document.getElementById("safetyReasonConfirm").addEventListener("click", function() {
      var reason = document.getElementById("safetyReasonInput").value.trim();
      if (!reason) {
        document.getElementById("safetyReasonError").style.display = "block";
        return;
      }
      document.body.removeChild(backdrop);
      callback(reason);
    });

    // Click backdrop to close
    backdrop.addEventListener("click", function(e) {
      if (e.target === backdrop) {
        document.body.removeChild(backdrop);
      }
    });
  };

  // ============================================================
  // Confirmation Dialog
  // ============================================================

  window.showConfirm = function(message, callback) {
    var backdrop = document.createElement("div");
    backdrop.className = "safety-reason-backdrop";
    backdrop.style.cssText = "position:fixed;inset:0;z-index:2000;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.35);backdrop-filter:blur(2px)";

    var dialog = document.createElement("div");
    dialog.className = "safety-reason-dialog card";
    dialog.style.cssText = "width:min(400px,90vw)";

    dialog.innerHTML = "<div class=\"card-hd\"><h2>确认操作</h2></div>" +
      "<div class=\"card-bd\" style=\"display:grid;gap:12px\">" +
        "<p style=\"margin:0;font-size:14px;line-height:1.6;color:var(--text)\">" + escapeHtml(message) + "</p>" +
        "<div style=\"display:flex;gap:8px;justify-content:flex-end\">" +
          "<button class=\"btn small secondary\" id=\"safetyConfirmCancel\">取消</button>" +
          "<button class=\"btn small danger\" id=\"safetyConfirmOk\">确认</button>" +
        "</div>" +
      "</div>";

    backdrop.appendChild(dialog);
    document.body.appendChild(backdrop);

    document.getElementById("safetyConfirmCancel").addEventListener("click", function() {
      document.body.removeChild(backdrop);
    });

    document.getElementById("safetyConfirmOk").addEventListener("click", function() {
      document.body.removeChild(backdrop);
      callback();
    });

    backdrop.addEventListener("click", function(e) {
      if (e.target === backdrop) {
        document.body.removeChild(backdrop);
      }
    });
  };

  // ============================================================
  // Error Display
  // ============================================================

  window.showError = function(message) {
    var existing = document.getElementById("safetyToast");
    if (existing) {
      document.body.removeChild(existing);
    }
    var toast = document.createElement("div");
    toast.id = "safetyToast";
    toast.style.cssText = "position:fixed;bottom:30px;left:50%;transform:translateX(-50%);z-index:3000;background:var(--danger-text);color:#fff;padding:12px 24px;border-radius:12px;font-size:14px;font-weight:700;box-shadow:0 8px 30px rgba(220,38,38,0.3);max-width:90vw;text-align:center";
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(function() {
      if (toast.parentNode) toast.parentNode.removeChild(toast);
    }, 4000);
  };

  // ============================================================
  // Action Submissions
  // ============================================================

  function _disableButton(groupId, actionType) {
    var selector = "button[data-action-id=\"" + actionType + "-" + groupId + "\"]";
    var btn = document.querySelector(selector);
    if (btn) btn.disabled = true;
  }

  function _enableButton(groupId, actionType) {
    var selector = "button[data-action-id=\"" + actionType + "-" + groupId + "\"]";
    var btn = document.querySelector(selector);
    if (btn) btn.disabled = false;
  }

  window.submitPauseAgent = function(groupId, sessionId) {
    if (_requestInFlight) return;
    showConfirm("确认暂停该组 Agent？暂停后该组不会继续收到 Agent 干预。", function() {
      requireReason("暂停 Agent", function(reason) {
        _doSubmit(groupId, sessionId, "pause-agent", reason);
      });
    });
  };

  window.submitResumeAgent = function(groupId, sessionId) {
    if (_requestInFlight) return;
    showConfirm("确认恢复该组 Agent？恢复后将遵循当前 Session Role 及预算限制。", function() {
      requireReason("恢复 Agent", function(reason) {
        _doSubmit(groupId, sessionId, "resume-agent", reason);
      });
    });
  };

  window.submitPauseSession = function(groupId, sessionId) {
    if (_requestInFlight) return;
    showConfirm("确认暂停该组会话？暂停后学生端可能无法继续提交或交互，具体取决于后端策略。", function() {
      requireReason("暂停会话", function(reason) {
        _doSubmit(groupId, sessionId, "pause-session", reason);
      });
    });
  };

  window.submitResumeSession = function(groupId, sessionId) {
    if (_requestInFlight) return;
    showConfirm("确认恢复该组会话？恢复后学生端操作将恢复正常。", function() {
      requireReason("恢复会话", function(reason) {
        _doSubmit(groupId, sessionId, "resume-session", reason);
      });
    });
  };

  function _doSubmit(groupId, sessionId, action, reason) {
    if (!sessionId) {
      showError("无法获取当前课次 ID，请刷新后重试。");
      return;
    }

    _requestInFlight = true;
    // Disable the action button
    var actionId = action + "-" + groupId;
    var btn = document.querySelector("button[data-action-id=\"" + actionId + "\"]");
    if (btn) btn.disabled = true;

    var url = "/api/teacher/group/" + groupId + "/" + action;
    fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, reason: reason })
    })
      .then(function(r) {
        if (!r.ok) {
          return r.json().then(function(errData) {
            throw new Error(errData.error || "请求失败: " + r.status);
          }).catch(function(e) {
            if (e instanceof SyntaxError) throw new Error("请求失败: " + r.status);
            throw e;
          });
        }
        return r.json();
      })
      .then(function(data) {
        _requestInFlight = false;
        if (btn) btn.disabled = false;
        // Refresh the status
        loadSafetyStatus();
        // Also refresh global status bar
        if (typeof gsFetchStatus === "function") {
          gsFetchStatus();
        }
      })
      .catch(function(err) {
        _requestInFlight = false;
        if (btn) btn.disabled = false;
        showError("操作失败: " + err.message);
      });
  }

  // ============================================================
  // Utility: formatDt (same as teacher-api.js)
  // ============================================================

  function formatDt(dtStr) {
    if (!dtStr) return "--";
    try {
      var d = new Date(dtStr.replace(" ", "T"));
      if (isNaN(d.getTime())) return dtStr;
      return d.toLocaleString("zh-CN", { hour12: false });
    } catch (_) { return dtStr; }
  }

  function escapeHtml(s) {
    return (s || "").replace(/[&<>"']/g, function(m) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#039;" })[m];
    });
  }

})();
