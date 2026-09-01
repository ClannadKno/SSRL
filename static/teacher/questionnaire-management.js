// Batch 10 questionnaire management frontend.
// Keeps existing teacher questionnaire APIs and exposes handlers used by inline buttons.
(function() {
  'use strict';

  var qmData = {};

  function byId(id) {
    return document.getElementById(id);
  }

  function html(value) {
    return window.escapeHtml ? window.escapeHtml(value || '') : String(value || '');
  }

  function setStatus(id, message, type) {
    var el = byId(id);
    if (!el) return;
    el.textContent = message || '';
    el.classList.toggle('ui-error-state', type === 'error');
  }

  function safeApiMessage(error) {
    return error && error.message ? error.message : '请求失败';
  }

  function renderEmpty(message) {
    return '<div class="ui-empty-state">' + html(message) + '</div>';
  }

  function renderError(message) {
    return '<div class="ui-error-state">' + html(message) + '</div>';
  }

  function option(value, text, extra) {
    return '<option value="' + html(value) + '"' + (extra || '') + '>' + html(text) + '</option>';
  }

  async function loadFixedQList() {
    try {
      var data = await window.fetchJSON('/api/teacher/questionnaires/fixed');
      qmData.fixed = data.questionnaires || [];
      var list = qmData.fixed;
      var rendered = list.length ? list.map(function(q) {
        var timingText = q.timing === 'both' ? '前后测' : (q.timing === 'pre' ? '前测' : '后测');
        var statusText = q.active ? '启用' : '停用';
        return '<div class="group-card">' +
          '<div class="group-card-head"><div><div class="group-title">' + html(q.title) + '</div>' +
          '<div class="muted" style="font-size:12px">' + html(q.code) + ' | ' + timingText + ' | 1-' + (q.scale_max || 5) + ' | ' + (q.section_count || 0) + '章节 | ' + (q.item_count || 0) + '题</div></div>' +
          '<span class="badge">' + statusText + '</span></div>' +
          '<div class="evidence">' + html(q.description) + '</div>' +
          '<div class="muted" style="font-size:12px">已发布课次: ' + (q.active_publication_count || 0) + '</div>' +
          '<div class="teacher-actions"><button class="btn small secondary" onclick="viewFixedDetail(' + q.id + ')">查看详情</button></div>' +
          '</div>';
      }).join('') : renderEmpty('暂无固定问卷。');
      byId('fixedQList').innerHTML = rendered;
    } catch (error) {
      qmData.fixed = [];
      byId('fixedQList').innerHTML = renderError('固定问卷加载失败：' + safeApiMessage(error));
    }
  }

  function viewFixedDetail(qid) {
    var q = (qmData.fixed || []).find(function(item) { return item.id === qid; });
    if (!q) return;
    var items = q.items || [];
    var itemHtml = items.length ? items.map(function(it, idx) {
      var reverseText = it.reverse_scored ? ' <span style="color:var(--danger-text)">反向题</span>' : '';
      return '<div style="padding:8px 0;border-bottom:1px solid var(--border-light)">' +
        '<div style="font-weight:600;font-size:13px">Q' + (idx + 1) + '. ' + html(it.prompt_text) + '</div>' +
        '<div class="muted" style="font-size:11px">维度: ' + html(it.dimension_label || '-') + ' | 编码: ' + html(it.item_code) + reverseText + '</div></div>';
    }).join('') : renderEmpty('暂无题目。');

    var modalHtml = '<div class="qm-detail-modal" onclick="if(event.target===this)this.remove()"><div>' +
      '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;gap:12px">' +
      '<h2 style="margin:0">' + html(q.title) + '</h2>' +
      '<button class="btn small secondary" onclick="this.closest(\'.qm-detail-modal\').remove()">关闭</button></div>' +
      '<div class="muted" style="font-size:12px;margin-bottom:12px">编码: ' + html(q.code) + ' | timing: ' + html(q.timing) + ' | 量表: 1-' + (q.scale_max || 5) + '</div>' +
      '<div style="margin-bottom:12px">' + html(q.description) + '</div>' +
      '<h3 style="margin:12px 0 8px">题目列表</h3>' + itemHtml + '</div></div>';
    var div = document.createElement('div');
    div.innerHTML = modalHtml;
    document.body.appendChild(div.firstElementChild);
  }

  async function loadPubData() {
    try {
      var data = await window.fetchJSON('/api/teacher/questionnaires/fixed');
      qmData.fixed = data.questionnaires || [];
      byId('pubQSelect').innerHTML = option('', '-- 选择固定问卷 --') + qmData.fixed.map(function(q) {
        return option(q.id, q.title || q.code || '');
      }).join('');
    } catch (error) {
      setStatus('pubStatus', '固定问卷加载失败：' + safeApiMessage(error), 'error');
    }
    try {
      var sessionsData = await window.fetchJSON('/api/teacher/sessions');
      qmData.sessions = sessionsData.sessions || [];
      byId('pubSessionSelect').innerHTML = option('', '-- 选择课次 --') + qmData.sessions.map(function(s) {
        return option(s.id, '课次 #' + (s.session_no || s.id) + ': ' + (s.title || ''), ' data-no="' + html(s.session_no || 0) + '"');
      }).join('');
    } catch (error) {
      setStatus('pubStatus', '课次加载失败：' + safeApiMessage(error), 'error');
    }
    loadPubList();
  }

  async function loadPubList() {
    try {
      var data = await window.fetchJSON('/api/teacher/questionnaire-publications');
      qmData.publications = data.publications || [];
      var list = qmData.publications;
      byId('pubList').innerHTML = list.length ? list.map(function(p) {
        var stageText = p.response_stage === 'pre' ? '前测' : '后测';
        var statusText = p.status === 'enabled' ? '已启用' : '已关闭';
        var toggleBtn = p.status === 'enabled'
          ? '<button class="btn small secondary" onclick="togglePub(' + p.id + ',\'closed\')">关闭</button>'
          : '<button class="btn small" onclick="togglePub(' + p.id + ',\'enabled\')">启用</button>';
        return '<div class="group-card"><div class="group-card-head"><div><div class="group-title">' + html(p.questionnaire_title) + '</div>' +
          '<div class="muted" style="font-size:12px">课次#' + (p.session_no || p.es_session_no || '') + ' | ' + stageText + ' | ' + statusText + '</div></div>' +
          '<div style="display:flex;gap:6px;flex-wrap:wrap">' + toggleBtn +
          '<button class="btn small secondary" onclick="deletePub(' + p.id + ')">删除</button></div></div></div>';
      }).join('') : renderEmpty('暂无发布记录。');
    } catch (error) {
      qmData.publications = [];
      byId('pubList').innerHTML = renderError('发布记录加载失败：' + safeApiMessage(error));
    }
  }

  async function createPublication() {
    var qid = byId('pubQSelect').value;
    var sid = byId('pubSessionSelect').value;
    var stage = byId('pubStageSelect').value;
    var selEl = byId('pubSessionSelect');
    var selected = selEl.options[selEl.selectedIndex];
    var sno = parseInt(selected ? selected.getAttribute('data-no') || '0' : '0', 10);
    if (!qid || !sid) {
      setStatus('pubStatus', '请选择问卷和课次');
      return;
    }
    try {
      await window.fetchJSON('/api/teacher/questionnaire-publications', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({questionnaire_id: parseInt(qid, 10), session_id: parseInt(sid, 10), session_no: sno, response_stage: stage})
      });
      setStatus('pubStatus', '发布成功！');
      loadPubList();
    } catch (error) {
      setStatus('pubStatus', '发布失败：' + safeApiMessage(error), 'error');
    }
  }

  async function togglePub(pid, status) {
    try {
      await window.fetchJSON('/api/teacher/questionnaire-publications/' + pid, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: status})
      });
      loadPubList();
    } catch (error) {
      alert('操作失败：' + safeApiMessage(error));
    }
  }

  async function deletePub(pid) {
    if (!confirm('确认删除此发布？如已有学生提交，将自动转为关闭状态。')) return;
    try {
      await window.fetchJSON('/api/teacher/questionnaire-publications/' + pid, {method: 'DELETE'});
      loadPubList();
    } catch (error) {
      alert('操作失败：' + safeApiMessage(error));
    }
  }

  async function loadCompletion() {
    try {
      var data = await window.fetchJSON('/api/teacher/questionnaire-completion');
      qmData.stats = data.stats || [];
      var list = qmData.stats;
      if (!list.length) {
        byId('compList').innerHTML = renderEmpty('暂无完成数据。');
        return;
      }
      byId('compList').innerHTML = '<div class="final-table-scroll"><table class="final-data-table"><thead><tr>' +
        '<th>课次</th><th>问卷</th><th>阶段</th><th>小组</th><th>已完成</th><th>应完成</th><th>未完成</th></tr></thead><tbody>' +
        list.map(function(r) {
          return '<tr><td>课次#' + (r.session_no || '') + '</td>' +
            '<td>' + html(r.questionnaire_title) + '</td>' +
            '<td>' + (r.response_stage === 'pre' ? '前测' : '后测') + '</td>' +
            '<td>' + html(r.group_name || r.group_code || '') + '</td>' +
            '<td>' + (r.completed_count || 0) + '</td>' +
            '<td>' + (r.roster_count || 0) + '</td>' +
            '<td>' + (r.uncompleted_count || 0) + '</td></tr>';
        }).join('') + '</tbody></table></div>';
    } catch (error) {
      qmData.stats = [];
      byId('compList').innerHTML = renderError('完成统计加载失败：' + safeApiMessage(error));
    }
  }

  function switchQmTab(name) {
    document.querySelectorAll('.qm-tab-content').forEach(function(el) { el.style.display = 'none'; });
    document.querySelectorAll('.qm-tab').forEach(function(el) {
      el.classList.add('secondary');
      el.setAttribute('aria-selected', 'false');
    });
    var showEl = byId('qmTab' + name.charAt(0).toUpperCase() + name.slice(1));
    if (showEl) showEl.style.display = '';
    var tabBtn = document.querySelector('.qm-tab[data-tab="' + name + '"]');
    if (tabBtn) {
      tabBtn.classList.remove('secondary');
      tabBtn.setAttribute('aria-selected', 'true');
    }
    if (name === 'fixed') loadFixedQList();
    if (name === 'publish') loadPubData();
    if (name === 'completion') loadCompletion();
    if (name === 'export') loadExportSessions();
  }

  async function loadExportSessions() {
    try {
      var data = await window.fetchJSON('/api/teacher/sessions');
      qmData.sessions = data.sessions || [];
      var sel = byId('exportSessionSelect');
      if (sel) {
        sel.innerHTML = option('', '-- 全部课次 --') + qmData.sessions.map(function(s) {
          return option(s.id, '课次 #' + (s.session_no || s.id) + ': ' + (s.title || ''));
        }).join('');
      }
    } catch (error) {
      setStatus('exportStatus', '课次加载失败：' + safeApiMessage(error), 'error');
    }
  }

  async function doExportQuestionnaireRaw() {
    var sel = byId('exportSessionSelect');
    var statusEl = byId('exportStatus');
    if (!statusEl) return;
    var sessionIds = sel ? sel.value : '';
    statusEl.textContent = '正在生成导出...';
    try {
      var params = new URLSearchParams();
      if (sessionIds) params.set('session_ids', sessionIds);
      var resp = await fetch('/api/teacher/questionnaire-raw-export?' + params.toString(), {headers: window.getDefaultHeaders ? window.getDefaultHeaders() : {}});
      if (!resp.ok) {
        var errData = await resp.json().catch(function() { return null; });
        throw new Error((errData && errData.error) || ('HTTP ' + resp.status));
      }
      var blob = await resp.blob();
      var disposition = resp.headers.get('Content-Disposition') || '';
      var match = disposition.match(/filename=(.+)/);
      var filename = match ? match[1].trim() : 'questionnaire_raw_export.zip';
      var link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      setTimeout(function() { URL.revokeObjectURL(link.href); }, 5000);
      setStatus('exportStatus', '导出成功！');
    } catch (error) {
      setStatus('exportStatus', '导出失败：' + safeApiMessage(error), 'error');
    }
  }

  window.loadFixedQList = loadFixedQList;
  window.viewFixedDetail = viewFixedDetail;
  window.loadPubData = loadPubData;
  window.loadPubList = loadPubList;
  window.createPublication = createPublication;
  window.togglePub = togglePub;
  window.deletePub = deletePub;
  window.loadCompletion = loadCompletion;
  window.switchQmTab = switchQmTab;
  window.loadExportSessions = loadExportSessions;
  window.doExportQuestionnaireRaw = doExportQuestionnaireRaw;

  document.addEventListener('DOMContentLoaded', function() { switchQmTab('fixed'); });
})();
