// SSRL-ESP Teacher API shared utilities
// Provides: fetchJSON, tab token, escapeHtml, shared display helpers

(function() {
  'use strict';

  const TAB_TOKEN = (function() {
    const urlToken = new URLSearchParams(window.location.search).get('tab_token');
    if (urlToken) sessionStorage.setItem('SSRL_ESP_TAB_TOKEN', urlToken);
    return sessionStorage.getItem('SSRL_ESP_TAB_TOKEN') || urlToken || '';
  })();

  function withTabToken(options) {
    options = options || {};
    const headers = Object.assign({}, options.headers || {});
    if (TAB_TOKEN) headers['X-Tab-Token'] = TAB_TOKEN;
    return Object.assign({}, options, { headers: headers });
  }

  window.SSRL_TAB_TOKEN = TAB_TOKEN;

  window.fetchJSON = async function(url, options) {
    const res = await fetch(url, withTabToken(options || {}));
    if (!res.ok) {
      let errMsg = 'Request failed: ' + res.status;
      try {
        const errData = await res.json();
        if (errData.error) errMsg = errData.error;
      } catch (_) {}
      throw new Error(errMsg);
    }
    return await res.json();
  };

  window.escapeHtml = function(s) {
    return (s || '').replace(/[&<>"']/g, function(m) {
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'})[m];
    });
  };

  window.formatPercent = function(value) {
    if (value == null || Number.isNaN(Number(value))) return '--';
    return Math.round(Number(value) * 100) + '%';
  };

  window.formatDuration = function(seconds) {
    const value = Number(seconds || 0);
    if (!value) return '不足 1 分钟';
    const minutes = Math.floor(value / 60);
    const hours = Math.floor(minutes / 60);
    const remainMinutes = minutes % 60;
    if (hours) return hours + ' 小时 ' + remainMinutes + ' 分钟';
    if (minutes) return minutes + ' 分钟';
    return '不足 1 分钟';
  };

  window.formatDt = function(dtStr) {
    if (!dtStr) return '--';
    try {
      const d = new Date(dtStr.replace(' ', 'T') + (dtStr.includes('Z') ? '' : ''));
      if (isNaN(d.getTime())) return dtStr;
      return d.toLocaleString('zh-CN', { hour12: false });
    } catch (_) { return dtStr; }
  };

  window.roleLabel = function(role) {
    if (role === 'teacher') return '教师';
    if (role === 'agent') return 'SERA';
    return '学生';
  };

  window.conditionLabel = function(condition) {
    return condition === 'control' ? '对照组' : '实验组';
  };

  window.riskClass = function(level) {
    if (level === 3) return 'high';
    if (level === 2) return 'mid';
    return 'low';
  };

  // Patch fetch to carry X-Tab-Token on all same-origin requests
  const origFetch = window.fetch;
  window.fetch = function(url, options) {
    options = options || {};
    if (typeof url === 'string' && url.startsWith('/') && !url.startsWith('//')) {
      const headers = Object.assign({}, options.headers || {});
      if (TAB_TOKEN && !headers['X-Tab-Token']) headers['X-Tab-Token'] = TAB_TOKEN;
      options.headers = headers;
    }
    return origFetch.call(window, url, options);
  };
})();
