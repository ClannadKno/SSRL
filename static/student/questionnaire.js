 /**
  * Questionnaire Module for SSRL-ESP
  * Provides type-specific renderers, validation, and atomic submission.
  * Loaded only for pretest/posttest phases.
  */
 window.Questionnaire = (function() {
   'use strict';
 
   // --- State ---
   var questionnaires = [];        // Full payload from API
   var openQuestionnaireId = null; // Currently active q id
   var currentStage = null;        // 'pre' or 'post'
   var isSubmitting = false;
   var modeLabel = '';            // '前测' or '后测'
   var mode = '';                 // 'pre' or 'post'
   var tabToken = '';
   var currentSessionId = '';
   var questionnaireStatus = 'ok';
   var questionnaireMessage = '';
   var postCheckinCompleted = false;
   var postCheckinSubmitting = false;
   var POST_CHECKIN_ID = '__post_emotion_checkin__';
   var DRAFT_STORAGE_PREFIX = 'ssrl_esp_questionnaire_draft:v1:';
 
   // --- Utilities ---
   function escapeHtml(s) {
     return (s || '').replace(/[&<>"']/g, function(m) {
       return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m];
     });
   }
 
   function sanitize(s) {
     return (s || '').replace(/[<>&]/g, function(m) {
       return {'&':'&amp;','<':'&lt;','>':'&gt;'}[m];
     });
   }
 
   function withTabToken(opts) {
     opts = opts || {};
     var h = Object.assign({}, opts.headers || {});
     if (tabToken) h['X-Tab-Token'] = tabToken;
     return Object.assign({}, opts, {headers: h});
   }
 
   function fetchJSON(url, opts) {
     return fetch(url, withTabToken(opts || {})).then(function(res) {
       if (!res.ok) throw new Error('Request failed');
       return res.json();
     });
   }
 
   function findQ(id) {
     for (var i = 0; i < questionnaires.length; i++) {
       if (String(questionnaires[i].id) === String(id)) return questionnaires[i];
     }
     return null;
   }

   function storageAvailable() {
     try {
       var key = DRAFT_STORAGE_PREFIX + 'probe';
       window.localStorage.setItem(key, '1');
       window.localStorage.removeItem(key);
       return true;
     } catch (e) {
       return false;
     }
   }

   function draftStorageKey(qid, stage) {
     return DRAFT_STORAGE_PREFIX + encodeURIComponent(tabToken || 'anonymous') + ':' +
       encodeURIComponent(currentSessionId || 'no-session') + ':' +
       encodeURIComponent(stage || mode || 'pre') + ':' + encodeURIComponent(qid);
   }

   function readQuestionnaireDraft(qid, stage) {
     if (!storageAvailable() || !qid) return null;
     try {
       var raw = window.localStorage.getItem(draftStorageKey(qid, stage));
       return raw ? JSON.parse(raw) : null;
     } catch (e) {
       return null;
     }
   }

   function writeQuestionnaireDraft(qid, stage, responses) {
     if (!storageAvailable() || !qid) return;
     try {
       window.localStorage.setItem(draftStorageKey(qid, stage), JSON.stringify({
         responses: responses || {},
         saved_at: new Date().toISOString()
       }));
     } catch (e) {
       // Browsers can deny or evict localStorage; the questionnaire remains submittable.
     }
   }

   function clearQuestionnaireDraft(qid, stage) {
     if (!storageAvailable() || !qid) return;
     try {
       window.localStorage.removeItem(draftStorageKey(qid, stage));
     } catch (e) {}
   }

   function normalizeCachedResponse(value) {
     if (value === null || value === undefined || value === '') return null;
     if (typeof value === 'object') {
       if (value.value !== undefined || value.text !== undefined || value.option_key !== undefined) {
         return value;
       }
       return null;
     }
     var numVal = parseInt(value, 10);
     if (!isNaN(numVal) && String(numVal) === String(value)) return {value: numVal};
     return {text: String(value)};
   }

   function responseForItem(q, stage, item) {
     var key = stage + ':' + item.id;
     var existing = normalizeCachedResponse((q.existing_responses || {})[key]);
     var draft = readQuestionnaireDraft(q.id, stage);
     if (draft && draft.responses && Object.prototype.hasOwnProperty.call(draft.responses, item.id)) {
       return normalizeCachedResponse(draft.responses[item.id]);
     }
     return existing;
   }

   function saveOpenQuestionnaireDraft() {
     var q = findQ(openQuestionnaireId);
     if (!q || isPostCheckin(q) || isCompleted(q) || !currentStage) return;
     writeQuestionnaireDraft(q.id, currentStage, collectResponses());
   }

   function isPostCheckin(q) {
     return q && q.id === POST_CHECKIN_ID;
   }

   function getApplicableList() {
     return questionnaires.filter(function(q) {
       return (q.allowed_stages || []).indexOf(mode) !== -1;
     });
   }

   function isCompleted(q) {
     return (q.completed_stages || []).indexOf(mode) !== -1;
   }

   function countQuestionnaireItems(q) {
     if (isPostCheckin(q)) return 5;
     var sections = q.sections || [];
     var count = 0;
     for (var i = 0; i < sections.length; i++) {
       count += (sections[i].items || []).length;
     }
     if (!count && q.items) count = q.items.length;
     return count;
   }
 
   function allDone() {
     var applicable = getApplicableList();
     return applicable.length > 0 && applicable.every(function(q) {
       return (q.completed_stages || []).indexOf(mode) !== -1;
     });
   }
 
   function getPendingList() {
     return getApplicableList().filter(function(q) {
       return !isCompleted(q);
     });
   }
 
   function getStageLabel(stage) {
     if (stage === 'pre') return '前测';
     return '后测';
     return stage === 'pre' ? '前测' : '后测';
   }
 
   // --- Sanitize and build HTML safely ---
   function buildDom(html) {
     var d = document.createElement('div');
     d.innerHTML = html;
     return d.firstElementChild;
   }
 
   // ============================================================
   // Renderer: TextQuestionRenderer
   // ============================================================
   function textQuestionHTML(item, existing) {
     var code = escapeHtml(item.item_code || '');
     var prompt = sanitize(item.prompt_text || '');
     var required = item.required !== false;
     var requiredMark = required ? '<span class="qa-required-mark">*</span>' : '';
     var existingVal = '';
     if (existing && existing.text) existingVal = sanitize(existing.text);
     var isMultiline = item.question_type === 'text' && item.max_value > 100;
     var tag = isMultiline ? 'textarea' : 'input';
     var extraAttrs = isMultiline
       ? 'class="qa-text-input qa-textarea" rows="3"'
       : 'class="qa-text-input" type="text"';
     return '<div class="qa-item" data-item-id="' + item.id + '" data-required="' + (required ? '1' : '0') + '">' +
       (code ? '<div class="qa-item-code">' + code + '</div>' : '') +
       '<div class="qa-item-prompt">' + prompt + requiredMark + '</div>' +
       '<' + tag + ' ' + extraAttrs + ' data-item-id="' + item.id + '" placeholder="请输入..."' +
       ' value="' + existingVal + '">' + (isMultiline ? existingVal : '') + '</' + tag + '>' +
       '</div>';
   }
 
   function collectTextValue(itemEl) {
     var input = itemEl.querySelector('input.qa-text-input, textarea.qa-text-input');
     if (!input) return null;
     var val = input.value.trim();
     return val || null;
   }
 
   function validateText(itemEl) {
     var val = collectTextValue(itemEl);
     var required = itemEl.dataset.required === '1';
     if (required && !val) return false;
     return true;
   }
 
   // ============================================================
   // Renderer: SingleChoiceRenderer (also used for SituationalChoice)
   // ============================================================
   function singleChoiceHTML(item, existing, isSituational) {
     var code = escapeHtml(item.item_code || '');
     var prompt = sanitize(item.prompt_text || '');
     var required = item.required !== false;
     var requiredMark = required ? '<span class="qa-required-mark">*</span>' : '';
     var options = item.options || [];
     var name = 'q' + item.id;
     var existingKey = existing ? (existing.option_key || '') : '';
     var cards = [];
     for (var i = 0; i < options.length; i++) {
       var opt = options[i];
       var optKey = '';
       var optLabel = '';
       if (typeof opt === 'string') {
         optKey = opt;
         optLabel = opt;
       } else if (typeof opt === 'object') {
         optKey = opt.key || opt.value || String(opt);
         optLabel = opt.label || opt.text || optKey;
       } else {
         optKey = String(opt);
         optLabel = String(opt);
       }
       var selected = (optKey === existingKey) ? ' selected' : '';
       var optDisplay = isSituational
         ? '<span class="qa-opt-label">' + sanitize(optKey) + '.</span> ' + sanitize(optLabel)
         : sanitize(optLabel);
       cards.push(
         '<label class="qa-radio-card' + selected + '" data-key="' + escapeHtml(optKey) + '">' +
         '<input type="radio" name="' + name + '" value="' + escapeHtml(optKey) + '"' +
         (selected ? ' checked' : '') + '>' +
         '<span class="qa-radio-dot"></span>' +
         '<span>' + optDisplay + '</span>' +
         '</label>'
       );
     }
     var extraClass = isSituational ? ' qa-situation-opt' : '';
     return '<div class="qa-item' + extraClass + '" data-item-id="' + item.id + '" data-required="' + (required ? '1' : '0') + '">' +
       (code ? '<div class="qa-item-code">' + code + '</div>' : '') +
       '<div class="qa-item-prompt">' + prompt + requiredMark + '</div>' +
       '<div class="qa-radio-group">' + cards.join('') + '</div>' +
       '</div>';
   }
 
   function collectSingleChoiceValue(itemEl) {
     var checked = itemEl.querySelector('input[type="radio"]:checked');
     return checked ? checked.value : null;
   }
 
  function validateSingleChoice(itemEl) {
    var required = itemEl.dataset.required === '1';
    if (required) return collectSingleChoiceValue(itemEl) !== null;
    return true;
  }

  function optionParts(opt) {
    var key = '';
    var label = '';
    if (typeof opt === 'string') {
      key = opt;
      label = opt;
    } else if (typeof opt === 'object' && opt) {
      key = opt.key || opt.value || String(opt);
      label = opt.label || opt.text || key;
    } else {
      key = String(opt);
      label = String(opt);
    }
    return {key: key, label: label};
  }

  function existingOptionKeys(existing) {
    if (!existing) return [];
    if (Array.isArray(existing.option_keys)) return existing.option_keys.map(String);
    if (Array.isArray(existing.values)) return existing.values.map(String);
    if (existing.option_key) return String(existing.option_key).split(',').map(function(v) { return v.trim(); }).filter(Boolean);
    if (existing.text) {
      try {
        var parsed = JSON.parse(existing.text);
        if (Array.isArray(parsed)) return parsed.map(String);
        if (parsed && Array.isArray(parsed.option_keys)) return parsed.option_keys.map(String);
      } catch (e) {}
    }
    return [];
  }

  function multipleChoiceHTML(item, existing) {
    var code = escapeHtml(item.item_code || '');
    var prompt = sanitize(item.prompt_text || '');
    var required = item.required !== false;
    var requiredMark = required ? '<span class="qa-required-mark">*</span>' : '';
    var options = item.options || [];
    var selectedKeys = existingOptionKeys(existing);
    var cards = [];
    for (var i = 0; i < options.length; i++) {
      var opt = optionParts(options[i]);
      var selected = selectedKeys.indexOf(String(opt.key)) !== -1;
      cards.push(
        '<label class="qa-checkbox-card' + (selected ? ' selected' : '') + '" data-key="' + escapeHtml(opt.key) + '">' +
        '<input type="checkbox" value="' + escapeHtml(opt.key) + '"' + (selected ? ' checked' : '') + '>' +
        '<span class="qa-checkbox-dot"></span>' +
        '<span>' + sanitize(opt.label) + '</span>' +
        '</label>'
      );
    }
    return '<div class="qa-item qa-multiple-choice" data-item-id="' + item.id + '" data-required="' + (required ? '1' : '0') + '">' +
      (code ? '<div class="qa-item-code">' + code + '</div>' : '') +
      '<div class="qa-item-prompt">' + prompt + requiredMark + '</div>' +
      '<div class="qa-checkbox-group">' + cards.join('') + '</div>' +
      '</div>';
  }

  function collectMultipleChoiceValue(itemEl) {
    var checked = itemEl.querySelectorAll('input[type="checkbox"]:checked');
    var values = [];
    for (var i = 0; i < checked.length; i++) values.push(checked[i].value);
    return values;
  }

  // ============================================================
  // Renderer: LikertRenderer (5-point and 7-point)
  // ============================================================
   function likertHTML(item, existing, scaleMin, scaleMax, scaleLabels) {
     var code = escapeHtml(item.item_code || '');
     var prompt = sanitize(item.prompt_text || '');
     var required = item.required !== false;
     var requiredMark = required ? '<span class="qa-required-mark">*</span>' : '';
     var minVal = scaleMin || item.min_value || 1;
     var maxVal = scaleMax || item.max_value || 5;
     var name = 'q' + item.id;
     var existingVal = existing ? existing.value : null;
     var labels = scaleLabels || [];
     var currentLabel = '';
     if (existingVal !== null && existingVal !== undefined) {
       var idx = existingVal - minVal;
       if (idx >= 0 && idx < labels.length && labels[idx]) {
         currentLabel = existingVal + ' / ' + sanitize(labels[idx]);
       } else {
         currentLabel = String(existingVal);
       }
     }
     var options = [];
     for (var v = minVal; v <= maxVal; v++) {
       var selected = (existingVal !== null && existingVal == v) ? ' selected' : '';
       var lbl = '';
       var li = v - minVal;
       if (li < labels.length && labels[li]) {
         lbl = '<span class="qa-likert-label">' + sanitize(labels[li]) + '</span>';
       }
       options.push(
         '<label class="qa-likert-option' + selected + '" data-value="' + v + '">' +
         '<input type="radio" name="' + name + '" value="' + v + '"' +
         (selected ? ' checked' : '') + '>' +
         '<span class="qa-likert-dot"></span>' +
         '<span class="qa-likert-value">' + v + '</span>' +
         lbl +
         '</label>'
       );
     }
     return '<div class="qa-item" data-item-id="' + item.id + '" data-required="' + (required ? '1' : '0') + '">' +
       (code ? '<div class="qa-item-code">' + code + '</div>' : '') +
       '<div class="qa-item-prompt">' + prompt + requiredMark + '</div>' +
       '<div class="qa-likert">' + options.join('') + '</div>' +
       '<div class="qa-likert-current">' + currentLabel + '</div>' +
       '</div>';
   }
 
   function collectLikertValue(itemEl) {
     var checked = itemEl.querySelector('input[type="radio"]:checked');
     return checked ? parseInt(checked.value, 10) : null;
   }
 
   function validateLikert(itemEl) {
     var required = itemEl.dataset.required === '1';
     if (required) return collectLikertValue(itemEl) !== null;
     return true;
   }
 
   // ============================================================
   // Renderer: MatrixLikertSectionRenderer
   // ============================================================
   function matrixLikertSectionHTML(section, stage, q) {
     var title = sanitize(section.title || '');
     var desc = sanitize(section.description || '');
     var items = section.items || [];
     var scaleMin = section.scale_min || 1;
     var scaleMax = section.scale_max || 5;
     var labels = section.scale_labels || [];
 
     // Build columns for scale points
     var colHeaders = [];
     for (var v = scaleMin; v <= scaleMax; v++) {
       var li = v - scaleMin;
       var lbl = (li < labels.length && labels[li]) ? sanitize(labels[li]) : '';
       colHeaders.push('<th>' + v + (lbl ? '<br><span style="font-weight:400;font-size:10px">' + lbl + '</span>' : '') + '</th>');
     }
 
     // Build rows for each item
     var rows = [];
     for (var i = 0; i < items.length; i++) {
       var item = items[i];
       var code = escapeHtml(item.item_code || '');
       var prompt = sanitize(item.prompt_text || '');
       var required = item.required !== false;
       var requiredMark = required ? '<span class="qa-required-mark">*</span>' : '';
       var name = 'q' + item.id;
       var existing = q ? responseForItem(q, stage, item) : null;
       var existingVal = existing ? existing.value : null;
       var cells = [];
       for (var v2 = scaleMin; v2 <= scaleMax; v2++) {
         var selected = (existingVal !== null && existingVal !== undefined && existingVal == v2);
         cells.push(
           '<td class="qa-matrix-cell">' +
           '<label class="qa-matrix-radio' + (selected ? ' selected' : '') + '" data-value="' + v2 + '">' +
           '<input type="radio" name="' + name + '" value="' + v2 + '"' + (selected ? ' checked' : '') + '>' +
           '</label>' +
           '</td>'
         );
       }
       rows.push(
         '<tr data-item-id="' + item.id + '" data-required="' + (required ? '1' : '0') + '">' +
         '<td class="qa-matrix-item-text">' +
         (code ? '<span class="qa-matrix-item-code">' + code + '</span>' : '') +
         prompt + requiredMark +
         '</td>' +
         cells.join('') +
         '</tr>'
       );
     }
 
     var headerRows = '<thead><tr><th></th>' + colHeaders.join('') + '</tr></thead>';
     var bodyRows = '<tbody>' + rows.join('') + '</tbody>';
 
     return '<div class="qa-section matrix-section">' +
       (title ? '<div class="qa-section-title">' + title + '</div>' : '') +
       (desc ? '<div class="qa-section-desc">' + desc + '</div>' : '') +
       '<div class="qa-matrix-wrapper"><div class="qa-matrix-scroll">' +
       '<table class="qa-matrix-table">' + headerRows + bodyRows + '</table>' +
       '</div></div>' +
       '<div class="qa-matrix-current" id="matrixCurrent_' + (section.section_key || i) + '"></div>' +
       '</div>';
   }
 
   // ============================================================
   // Main rendering
   // ============================================================
 
   function renderQuestionnaireList() {
     var summary = document.getElementById('questionnaireSummary');
     var formBox = document.getElementById('questionnaireFormBox');
     var doneBox = document.getElementById('questionnaireCompletionBox');
     if (!summary) return;
 
     // Show all-done state
     if (allDone()) {
       summary.innerHTML = '<div class="qa-completed">' +
         '<div class="qa-completed-icon">&#10003;</div>' +
         '<h3>全部' + getStageLabel(mode) + '问卷已完成</h3>' +
         '<p>感谢您的作答</p></div>';
       if (formBox) formBox.innerHTML = '';
       if (doneBox) {
         doneBox.style.display = 'block';
         doneBox.innerHTML = '<div style="text-align:center;margin-top:8px">' +
           '<a href="?phase=discussion&tab_token=' + encodeURIComponent(tabToken) + '" class="submission-btn">返回讨论</a>' +
           '</div>';
       }
       return;
     }
 
     if (doneBox) doneBox.style.display = 'none';
 
     // Render list of pending questionnaires
     var pending = getPendingList();
     if (pending.length === 0) {
       summary.innerHTML = '<div class="qa-info-msg">当前课时暂无已启用的' + getStageLabel(mode) + '问卷</div>';
       if (formBox) formBox.innerHTML = '';
       return;
     }
 
     var listHtml = pending.map(function(q) {
       var active = (q.id === openQuestionnaireId) ? ' active' : '';
       return '<button class="qa-list-btn' + active + '" data-qid="' + q.id + '">' +
         '<span>' + escapeHtml(q.title || '') + '</span>' +
         '</button>';
     }).join('');
     summary.innerHTML = '<div class="qa-list">' + listHtml + '</div>';
 
     // Attach click handlers
     var btns = summary.querySelectorAll('.qa-list-btn');
     for (var i = 0; i < btns.length; i++) {
       btns[i].addEventListener('click', function(e) {
         var qid = parseInt(this.dataset.qid, 10);
         openQuestionnaire(qid);
       });
     }
 
     // Render currently open questionnaire
     if (openQuestionnaireId) {
       renderOpenQuestionnaire();
     } else if (pending.length > 0) {
       // Auto-open first pending
       openQuestionnaire(pending[0].id);
     }
   }
 
   function openQuestionnaire(qid) {
     saveOpenQuestionnaireDraft();
     openQuestionnaireId = qid;
     renderQuestionnaireList();
   }
 
   function renderOpenQuestionnaire() {
     var formBox = document.getElementById('questionnaireFormBox');
     if (!formBox) return;
     var q = findQ(openQuestionnaireId);
     if (!q) {
       formBox.innerHTML = '<div class="qa-info-msg">该问卷已不可用</div>';
       return;
     }
 
     var stage = mode;
     var sections = q.sections || [];
     var instruction = stage === 'pre'
       ? (q.instruction_pre || '')
       : (q.instruction_post || q.instruction_pre || '');
 
     var parts = [];
     parts.push(
       '<div class="qa-questionnaire-header">' +
       '<h2>' + escapeHtml(q.title || '') + '</h2>' +
       (instruction ? '<div class="qa-instruction">' + sanitize(instruction) + '</div>' : '') +
       '</div>'
     );
 
     // Render each section
     for (var si = 0; si < sections.length; si++) {
       var sec = sections[si];
       var displayType = (sec.display_type || 'standard').toLowerCase();
       var secItems = sec.items || [];
       var sectionKey = sec.section_key || ('section_' + si);
 
       if (displayType === 'matrix_likert') {
         // Matrix Likert section
         parts.push(matrixLikertSectionHTML(sec, stage, q));
       } else {
         // Standard section: render each item with type-specific renderer
         if (sec.title || sec.description) {
           parts.push('<div class="qa-section" data-section-key="' + escapeHtml(sectionKey) + '">');
           if (sec.title) parts.push('<div class="qa-section-title">' + sanitize(sec.title) + '</div>');
           if (sec.description) parts.push('<div class="qa-section-desc">' + sanitize(sec.description) + '</div>');
         } else {
           parts.push('<div class="qa-section" data-section-key="' + escapeHtml(sectionKey) + '">');
         }
         for (var ii = 0; ii < secItems.length; ii++) {
           var item = secItems[ii];
           var existing = responseForItem(q, stage, item);
           var qtype = (item.question_type || 'likert_5').toLowerCase();
           var html = '';
          if (qtype === 'text' || qtype === 'number') {
            html = textQuestionHTML(item, existing);
          } else if (qtype === 'single_choice') {
            html = singleChoiceHTML(item, existing, false);
          } else if (qtype === 'multiple_choice' || qtype === 'multi_choice' || qtype === 'checkbox') {
            html = multipleChoiceHTML(item, existing);
          } else if (qtype === 'situational_choice' || qtype === 'scenario') {
            html = singleChoiceHTML(item, existing, true);
          } else if (qtype === 'likert' || qtype === 'likert_5') {
             html = likertHTML(item, existing, sec.scale_min || 1, sec.scale_max || 5, sec.scale_labels);
           } else if (qtype === 'likert_7') {
             html = likertHTML(item, existing, sec.scale_min || 1, sec.scale_max || 7, sec.scale_labels);
           } else {
             html = likertHTML(item, existing, sec.scale_min || 1, sec.scale_max || 5, sec.scale_labels);
           }
           parts.push(html);
         }
         parts.push('</div>');
       }
     }
 
     // Submit button area
     parts.push(
       '<div class="qa-submit-area">' +
       '<div class="qa-submit-row">' +
       '<button class="submission-btn qa-submit-btn" id="qaSubmitBtn" onclick="window.Questionnaire.submitQuestionnaire()">提交' + getStageLabel(stage) + '</button>' +
       '<span id="qaSubmitStatus"></span>' +
       '</div>' +
       '<div id="qaErrorContainer"></div>' +
       '</div>'
     );
 
     formBox.innerHTML = parts.join('');
     attachItemEventListeners();
   }

   function normalizeSections(q) {
     if (q.sections && q.sections.length) return q.sections;
     var items = q.items || [];
     if (!items.length) return [];
     var buckets = [];
     var byKey = {};
     for (var i = 0; i < items.length; i++) {
        var item = items[i];
        var key = item.section_no || item.section_key || 'default';
        var itemType = (item.question_type || '').toLowerCase();
        if (!byKey[key]) {
          byKey[key] = {
            section_key: String(key),
            title: item.section_title || '',
            description: item.section_description || '',
            display_type: item.display_type || (itemType === 'matrix_likert' ? 'matrix_likert' : 'standard'),
            scale_min: item.scale_min || q.scale_min || 1,
            scale_max: item.scale_max || q.scale_max || 5,
            scale_labels: item.scale_labels || q.scale_labels || [],
            items: []
          };
          buckets.push(byKey[key]);
        }
        if (itemType === 'matrix_likert') {
          byKey[key].display_type = 'matrix_likert';
        }
        byKey[key].items.push(item);
      }
     return buckets;
   }

   function renderQuestionnaireNav(applicable, completedCount, pendingCount) {
     var total = applicable.length;
     var percent = total ? Math.round((completedCount / total) * 100) : 0;
     var listHtml = applicable.map(function(q, index) {
       var done = isCompleted(q);
       var active = String(q.id) === String(openQuestionnaireId);
       var classes = 'qa-list-btn' + (active ? ' active' : '') + (done ? ' completed' : '');
       var status = done ? '已完成' : (active ? '填写中' : '待填写');
       var disabled = done ? ' disabled aria-disabled="true"' : '';
       return '<button class="' + classes + '" data-qid="' + q.id + '"' + disabled + '>' +
         '<span class="qa-nav-index">' + (done ? '&#10003;' : (index + 1)) + '</span>' +
         '<span class="qa-nav-main">' +
         '<span class="qa-nav-title">' + escapeHtml(q.title || ('问卷 ' + (index + 1))) + '</span>' +
         '<span class="qa-nav-meta">' + countQuestionnaireItems(q) + ' 题 · ' + status + '</span>' +
         '</span>' +
         '</button>';
     }).join('');
     return '<div class="qa-nav-panel">' +
       '<div class="qa-progress-card">' +
       '<div class="qa-progress-top"><span>' + getStageLabel(mode) + '进度</span><strong>' + completedCount + '/' + total + '</strong></div>' +
       '<div class="qa-progress-track"><span style="width:' + percent + '%"></span></div>' +
       '<div class="qa-progress-note">' + (pendingCount ? '还有 ' + pendingCount + ' 份问卷待完成' : '本阶段问卷已完成') + '</div>' +
       '</div>' +
       '<div class="qa-list">' + listHtml + '</div>' +
       '</div>';
   }

   function renderDoneState(formBox) {
     formBox.innerHTML = '<div class="qa-state-card qa-completed">' +
       '<div class="qa-completed-icon">&#10003;</div>' +
       '<h3>全部' + getStageLabel(mode) + '问卷已完成</h3>' +
       '<p>感谢你的认真作答，可以回到讨论继续协作。</p>' +
       '<a href="?phase=discussion&tab_token=' + encodeURIComponent(tabToken) + '" class="submission-btn">返回讨论</a>' +
       '</div>';
   }

   function renderEmptyState(formBox) {
     formBox.innerHTML = '<div class="qa-state-card">' +
       '<h3>当前没有需要填写的' + getStageLabel(mode) + '问卷</h3>' +
       '<p>如果老师刚刚发布了问卷，可以点击右上角刷新。</p>' +
       '</div>';
   }

   function renderWaitingSessionState(formBox) {
     formBox.innerHTML = '<div class="qa-state-card">' +
       '<h3>等待教师设置课时</h3>' +
       '<p>课时设置完成后，系统会显示与你所在课次、小组和个人匹配的问卷。</p>' +
       '</div>';
   }

   function renderQuestionnaireList() {
     var summary = document.getElementById('questionnaireSummary');
     var formBox = document.getElementById('questionnaireFormBox');
     var doneBox = document.getElementById('questionnaireCompletionBox');
     if (!summary || !formBox) return;
     if (doneBox) {
       doneBox.style.display = 'none';
       doneBox.innerHTML = '';
     }

     if (questionnaireStatus === 'waiting_session') {
       summary.innerHTML = '<div class="qa-nav-panel"><div class="qa-progress-card">' +
         '<div class="qa-progress-top"><span>' + escapeHtml(questionnaireMessage || '等待教师设置课时') + '</span><strong>0/0</strong></div>' +
         '<div class="qa-progress-track"><span style="width:0%"></span></div>' +
         '<div class="qa-progress-note">课时设置完成后再填写问卷</div>' +
         '</div></div>';
       openQuestionnaireId = null;
       renderWaitingSessionState(formBox);
       return;
     }

     var applicable = getApplicableList();
     var pending = getPendingList();
     var completedCount = applicable.length - pending.length;

     if (applicable.length === 0) {
       summary.innerHTML = renderQuestionnaireNav([], 0, 0);
       openQuestionnaireId = null;
       renderEmptyState(formBox);
       return;
     }

     if (openQuestionnaireId) {
       var opened = findQ(openQuestionnaireId);
       if (!opened || isCompleted(opened)) openQuestionnaireId = null;
     }
     if (!openQuestionnaireId && pending.length > 0) {
       openQuestionnaireId = pending[0].id;
     }

     summary.innerHTML = renderQuestionnaireNav(applicable, completedCount, pending.length);

     var btns = summary.querySelectorAll('.qa-list-btn:not([disabled])');
     for (var i = 0; i < btns.length; i++) {
       btns[i].addEventListener('click', function() {
         openQuestionnaire(this.dataset.qid);
       });
     }

     if (allDone()) {
       renderDoneState(formBox);
       return;
     }
     renderOpenQuestionnaire();
   }

   function openQuestionnaire(qid) {
     saveOpenQuestionnaireDraft();
     openQuestionnaireId = qid;
     currentStage = mode;
     renderQuestionnaireList();
   }

   function renderPostCheckinQuestionnaire(formBox) {
     var emotionOptions = [
       ['smooth', '🙂 平稳'],
       ['stuck', '🧭 卡住'],
       ['conflict', '⚡ 分歧'],
       ['silent', '… 沉默'],
       ['frustrated', '△ 挫败']
     ];
     var optionHtml = emotionOptions.map(function(opt, index) {
       return '<button type="button" class="qa-checkin-emotion' + (index === 0 ? ' active' : '') + '" data-value="' + opt[0] + '">' +
         escapeHtml(opt[1]) +
         '</button>';
     }).join('');
     var scaleOptions = '';
     for (var v = 1; v <= 5; v++) {
       scaleOptions += '<option value="' + v + '"' + (v === 3 ? ' selected' : '') + '>' + v + '</option>';
     }
     formBox.innerHTML =
       '<div class="qa-paper qa-checkin-paper">' +
       '<div class="qa-questionnaire-header">' +
       '<div><div class="qa-kicker">后测问卷</div><h2>讨论后情绪打卡</h2></div>' +
       '<span class="qa-count-chip">5 题</span>' +
       '</div>' +
       '<div class="qa-instruction">请根据刚才的小组讨论，记录你此刻的主观感受。</div>' +
       '<div class="qa-section qa-checkin-section">' +
       '<div class="qa-item">' +
       '<div class="qa-item-prompt">当前感受<span class="qa-required-mark">*</span></div>' +
       '<div class="qa-checkin-emotions" id="postCheckinEmotionOptions">' + optionHtml + '</div>' +
       '</div>' +
       '<div class="qa-checkin-scales">' +
       '<label class="qa-checkin-scale"><span>积极程度</span><select id="postCheckinPositivity">' + scaleOptions + '</select></label>' +
       '<label class="qa-checkin-scale"><span>任务投入</span><select id="postCheckinEngagement">' + scaleOptions + '</select></label>' +
       '<label class="qa-checkin-scale"><span>小组氛围</span><select id="postCheckinAtmosphere">' + scaleOptions + '</select></label>' +
       '<label class="qa-checkin-scale"><span>表达意愿</span><select id="postCheckinExpression">' + scaleOptions + '</select></label>' +
       '</div>' +
       '<div class="qa-item">' +
       '<div class="qa-item-prompt">补充说明</div>' +
       '<textarea class="qa-text-input qa-textarea" id="postCheckinNote" placeholder="可不填，例如：这次讨论中最顺利或最困难的地方"></textarea>' +
       '</div>' +
       '</div>' +
       '</div>' +
       '<div class="qa-submit-area">' +
       '<div class="qa-submit-row">' +
       '<span class="qa-submit-note">默认作为讨论后主观体验记录</span>' +
       '<button class="submission-btn qa-submit-btn" id="qaSubmitBtn" onclick="window.Questionnaire.submitPostEmotionCheckin()">提交打卡</button>' +
       '<span id="qaSubmitStatus"></span>' +
       '</div>' +
       '<div id="qaErrorContainer"></div>' +
       '</div>';
     attachPostCheckinListeners();
   }

   function attachPostCheckinListeners() {
     var buttons = document.querySelectorAll('.qa-checkin-emotion');
     for (var i = 0; i < buttons.length; i++) {
       buttons[i].addEventListener('click', function() {
         var siblings = document.querySelectorAll('.qa-checkin-emotion');
         for (var j = 0; j < siblings.length; j++) siblings[j].classList.remove('active');
         this.classList.add('active');
       });
     }
   }

   function selectedPostCheckinEmotion() {
     var active = document.querySelector('.qa-checkin-emotion.active');
     return active ? active.dataset.value : 'smooth';
   }

   function postCheckinSelectValue(id) {
     var el = document.getElementById(id);
     return el ? parseInt(el.value, 10) : 3;
   }

   function setPostCheckinError(message) {
     var errorContainer = document.getElementById('qaErrorContainer');
     if (errorContainer) {
       errorContainer.innerHTML = message ? '<div class="qa-error-msg">' + escapeHtml(message) + '</div>' : '';
     }
   }

   function submitPostEmotionCheckin() {
     if (postCheckinSubmitting) return;
     var groupId = (typeof GROUP_ID !== 'undefined') ? GROUP_ID : null;
     if (!groupId) {
       setPostCheckinError('未找到小组信息，请刷新页面后重试。');
       return;
     }
     var btn = document.getElementById('qaSubmitBtn');
     var statusEl = document.getElementById('qaSubmitStatus');
     var noteEl = document.getElementById('postCheckinNote');
     var payload = {
       group_id: groupId,
       emotion_option: selectedPostCheckinEmotion(),
       positivity: postCheckinSelectValue('postCheckinPositivity'),
       engagement: postCheckinSelectValue('postCheckinEngagement'),
       atmosphere: postCheckinSelectValue('postCheckinAtmosphere'),
       expression_willingness: postCheckinSelectValue('postCheckinExpression'),
       note: noteEl ? noteEl.value.trim() : '',
       checkin_type: 'post'
     };
     setPostCheckinError('');
     if (btn) btn.disabled = true;
     if (statusEl) statusEl.textContent = '提交中...';
     postCheckinSubmitting = true;
     fetch('/api/checkin', withTabToken({
       method: 'POST',
       headers: {'Content-Type': 'application/json'},
       body: JSON.stringify(payload)
     }))
     .then(function(res) {
       if (!res.ok) {
         return res.json().catch(function() { return {}; }).then(function(data) {
           throw new Error(data.error || '提交失败，请重试');
         });
       }
       return res.json();
     })
     .then(function() {
       postCheckinCompleted = true;
       openQuestionnaireId = null;
       currentStage = null;
       return loadQuestionnaires();
     })
     .catch(function(err) {
       setPostCheckinError(err.message || '提交失败，请重试');
     })
     .finally(function() {
       if (btn) btn.disabled = false;
       if (statusEl) statusEl.textContent = '';
       postCheckinSubmitting = false;
     });
   }

   function renderOpenQuestionnaire() {
     var formBox = document.getElementById('questionnaireFormBox');
     if (!formBox) return;
     var q = findQ(openQuestionnaireId);
     if (!q || isCompleted(q)) {
       renderEmptyState(formBox);
       return;
     }
     if (isPostCheckin(q)) {
       currentStage = mode;
       renderPostCheckinQuestionnaire(formBox);
       return;
     }

     currentStage = mode;
     var stage = mode;
     var sections = normalizeSections(q);
     var instruction = stage === 'pre'
       ? (q.instruction_pre || q.description || '')
       : (q.instruction_post || q.instruction_pre || q.description || '');
     var totalItems = countQuestionnaireItems(q);
     var parts = [];

     parts.push(
       '<div class="qa-paper">' +
       '<div class="qa-questionnaire-header">' +
       '<div>' +
       '<div class="qa-kicker">' + getStageLabel(stage) + '问卷</div>' +
       '<h2>' + escapeHtml(q.title || '') + '</h2>' +
       '</div>' +
       '<span class="qa-count-chip">' + totalItems + ' 题</span>' +
       '</div>' +
       (instruction ? '<div class="qa-instruction">' + sanitize(instruction) + '</div>' : '')
     );

     for (var si = 0; si < sections.length; si++) {
       var sec = sections[si];
       var displayType = (sec.display_type || 'standard').toLowerCase();
       var secItems = sec.items || [];
       var sectionKey = sec.section_key || ('section_' + si);

       if (displayType === 'matrix_likert') {
         parts.push(matrixLikertSectionHTML(sec, stage, q));
       } else {
         parts.push('<div class="qa-section" data-section-key="' + escapeHtml(sectionKey) + '">');
         if (sec.title) parts.push('<div class="qa-section-title">' + sanitize(sec.title) + '</div>');
         if (sec.description) parts.push('<div class="qa-section-desc">' + sanitize(sec.description) + '</div>');
         for (var ii = 0; ii < secItems.length; ii++) {
           var item = secItems[ii];
           var existing = responseForItem(q, stage, item);
           var qtype = (item.question_type || 'likert_5').toLowerCase();
            if (qtype === 'text' || qtype === 'number') {
              parts.push(textQuestionHTML(item, existing));
            } else if (qtype === 'single_choice') {
              parts.push(singleChoiceHTML(item, existing, false));
            } else if (qtype === 'multiple_choice' || qtype === 'multi_choice' || qtype === 'checkbox') {
              parts.push(multipleChoiceHTML(item, existing));
            } else if (qtype === 'situational_choice' || qtype === 'scenario') {
              parts.push(singleChoiceHTML(item, existing, true));
            } else if (qtype === 'likert_7') {
             parts.push(likertHTML(item, existing, sec.scale_min || 1, sec.scale_max || 7, sec.scale_labels));
           } else {
             parts.push(likertHTML(item, existing, sec.scale_min || 1, sec.scale_max || 5, sec.scale_labels));
           }
         }
         parts.push('</div>');
       }
     }

     parts.push(
       '</div>' +
       '<div class="qa-submit-area">' +
       '<div class="qa-submit-row">' +
       '<span class="qa-submit-note">完成全部必答题后提交</span>' +
       '<button class="submission-btn qa-submit-btn" id="qaSubmitBtn" onclick="window.Questionnaire.submitQuestionnaire()">提交' + getStageLabel(stage) + '</button>' +
       '<span id="qaSubmitStatus"></span>' +
       '</div>' +
       '<div id="qaErrorContainer"></div>' +
       '</div>'
     );

     formBox.innerHTML = parts.join('');
     attachItemEventListeners();
   }

   function syncRadioVisualState(input) {
     if (!input || input.type !== 'radio') return;
     var name = input.name;
     var siblings = document.querySelectorAll('input[name="' + name + '"]');
     for (var i = 0; i < siblings.length; i++) {
       var wrapper = siblings[i].closest('.qa-radio-card, .qa-likert-option, .qa-matrix-radio');
       if (wrapper) wrapper.classList.toggle('selected', siblings[i].checked);
     }
     var item = input.closest('.qa-item');
     if (item) {
       var currentDisplay = item.querySelector('.qa-likert-current');
       if (currentDisplay) {
         var selected = item.querySelector('input[type="radio"]:checked');
         if (selected) {
           var option = selected.closest('.qa-likert-option');
           var lblEl = option ? option.querySelector('.qa-likert-label') : null;
           var lbl = lblEl ? lblEl.textContent.trim() : '';
           currentDisplay.textContent = selected.value + (lbl ? ' / ' + lbl : '');
         }
       }
     }
   }
 
   function attachItemEventListeners() {
     // Radio card click: select card
     var cards = document.querySelectorAll('.qa-radio-card');
     for (var i = 0; i < cards.length; i++) {
       cards[i].addEventListener('click', function(e) {
         if (e.target.tagName === 'INPUT') return;
         var input = this.querySelector('input[type="radio"]');
         if (input) {
           input.checked = true;
           var name = input.name;
           var siblings = document.querySelectorAll('input[name="' + name + '"]');
           for (var j = 0; j < siblings.length; j++) {
             var card = siblings[j].closest('.qa-radio-card');
             if (card) card.classList.remove('selected');
           }
           this.classList.add('selected');
           clearError(this.closest('.qa-item'));
           saveOpenQuestionnaireDraft();
         }
       });
     }
 
     // Likert option click
     var likertOpts = document.querySelectorAll('.qa-likert-option');
     for (var k = 0; k < likertOpts.length; k++) {
       likertOpts[k].addEventListener('click', function(e) {
         if (e.target.tagName === 'INPUT') return;
         var input = this.querySelector('input[type="radio"]');
         if (input) {
           input.checked = true;
           var name = input.name;
           var siblings = document.querySelectorAll('input[name="' + name + '"]');
           for (var j2 = 0; j2 < siblings.length; j2++) {
             var opt = siblings[j2].closest('.qa-likert-option');
             if (opt) opt.classList.remove('selected');
           }
           this.classList.add('selected');
           // Update current display
           var currentDisplay = this.closest('.qa-item').querySelector('.qa-likert-current');
           if (currentDisplay) {
             var val = input.value;
             var lblEl = this.querySelector('.qa-likert-label');
             var lbl = lblEl ? lblEl.textContent.trim() : '';
             currentDisplay.textContent = val + (lbl ? ' / ' + lbl : '');
           }
           clearError(this.closest('.qa-item'));
           saveOpenQuestionnaireDraft();
         }
       });
     }
 
     // Matrix radio click
     var matrixRadios = document.querySelectorAll('.qa-matrix-radio');
     for (var m = 0; m < matrixRadios.length; m++) {
       matrixRadios[m].addEventListener('click', function(e) {
         if (e.target.tagName === 'INPUT') return;
         var input = this.querySelector('input[type="radio"]');
         if (input) {
           input.checked = true;
           var name = input.name;
           var siblings = document.querySelectorAll('input[name="' + name + '"]');
           for (var j3 = 0; j3 < siblings.length; j3++) {
             var r = siblings[j3].closest('.qa-matrix-radio');
             if (r) r.classList.remove('selected');
           }
           this.classList.add('selected');
           clearError(this.closest('tr'));
           saveOpenQuestionnaireDraft();
         }
       });
     }

     var checkboxCards = document.querySelectorAll('.qa-checkbox-card');
     for (var cb = 0; cb < checkboxCards.length; cb++) {
       checkboxCards[cb].addEventListener('click', function(e) {
         var input = this.querySelector('input[type="checkbox"]');
         if (!input) return;
         if (e.target !== input) {
           e.preventDefault();
           input.checked = !input.checked;
         }
         this.classList.toggle('selected', input.checked);
         clearError(this.closest('.qa-item'));
         saveOpenQuestionnaireDraft();
       });
     }
 
     // Text input blur - clear error
     var textInputs = document.querySelectorAll('.qa-text-input');
     for (var t = 0; t < textInputs.length; t++) {
       textInputs[t].addEventListener('input', function() {
         clearError(this.closest('.qa-item'));
         saveOpenQuestionnaireDraft();
       });
     }

     var formBox = document.getElementById('questionnaireFormBox');
     if (formBox && !formBox._questionnaireDraftChangeBound) {
       formBox.addEventListener('change', function(e) {
         var target = e.target;
         if (!target) return;
         if (target.matches('input[type="radio"]')) {
           syncRadioVisualState(target);
           clearError(target.closest('.qa-item, tr'));
         }
         if (target.matches('input[type="checkbox"]')) {
           var checkboxCard = target.closest('.qa-checkbox-card');
           if (checkboxCard) checkboxCard.classList.toggle('selected', target.checked);
           clearError(target.closest('.qa-item'));
         }
         if (target.matches('input, textarea, select')) {
           saveOpenQuestionnaireDraft();
         }
       });
       formBox._questionnaireDraftChangeBound = true;
     }
  
     // Keyboard focus styles
     var focusable = document.querySelectorAll('.qa-radio-card, .qa-checkbox-card, .qa-likert-option, .qa-matrix-radio, .qa-text-input');
     for (var f = 0; f < focusable.length; f++) {
       focusable[f].addEventListener('focusin', function() {
         var parent = this.closest('.qa-item, tr');
         if (parent) parent.classList.add('is-focused');
       });
       focusable[f].addEventListener('focusout', function() {
         var parent = this.closest('.qa-item, tr');
         if (parent) parent.classList.remove('is-focused');
       });
     }
   }
 
   function clearError(el) {
     if (!el) return;
     el.classList.remove('has-error');
   }
 
   // ============================================================
   // Validation
   // ============================================================
   function validateAll() {
     var firstError = null;
     var errors = [];
 
     // Check standard items (in .qa-item containers)
     var items = document.querySelectorAll('.qa-item[data-item-id]');
     for (var i = 0; i < items.length; i++) {
       var itemEl = items[i];
       var itemId = parseInt(itemEl.dataset.itemId, 10);
       var required = itemEl.dataset.required === '1';
       if (!required) continue;
       var checkboxes = itemEl.querySelectorAll('input[type="checkbox"]');
       if (checkboxes.length > 0) {
         var hasChecked = false;
         for (var c = 0; c < checkboxes.length; c++) {
           if (checkboxes[c].checked) { hasChecked = true; break; }
         }
         if (!hasChecked) {
           itemEl.classList.add('has-error');
           errors.push({itemId: itemId, el: itemEl});
           if (!firstError) firstError = itemEl;
         }
         continue;
       }
       var input = itemEl.querySelector('input[type="radio"]:checked, input.qa-text-input, textarea.qa-text-input');
       var hasValue = false;
       if (input) {
         if (input.type === 'radio') hasValue = true;
         else hasValue = input.value.trim() !== '';
       }
       // For radio groups, check if any radio is checked
       var radios = itemEl.querySelectorAll('input[type="radio"]');
       if (radios.length > 0) {
         hasValue = false;
         for (var r = 0; r < radios.length; r++) {
           if (radios[r].checked) { hasValue = true; break; }
         }
       }
       if (!hasValue) {
         itemEl.classList.add('has-error');
         errors.push({itemId: itemId, el: itemEl});
         if (!firstError) firstError = itemEl;
       }
     }
 
     // Check matrix rows (in tr[data-item-id])
     var matrixRows = document.querySelectorAll('tr[data-item-id]');
     for (var mi = 0; mi < matrixRows.length; mi++) {
       var row = matrixRows[mi];
       var required2 = row.dataset.required === '1';
       if (!required2) continue;
       var checkedRadio = row.querySelector('input[type="radio"]:checked');
       if (!checkedRadio) {
         row.classList.add('has-error');
         errors.push({itemId: parseInt(row.dataset.itemId, 10), el: row});
         if (!firstError) firstError = row;
       }
     }
 
     if (firstError) {
       firstError.scrollIntoView({behavior: 'smooth', block: 'center'});
     }
 
     return errors;
   }
 
   // ============================================================
   // Collect responses
   // ============================================================
   function collectResponses() {
     var responses = {};
 
     // Standard items
     var items = document.querySelectorAll('.qa-item[data-item-id]');
     for (var i = 0; i < items.length; i++) {
       var itemEl = items[i];
       var itemId = parseInt(itemEl.dataset.itemId, 10);
       if (!itemId) continue;

       var checkboxInputs = itemEl.querySelectorAll('input[type="checkbox"]:checked');
       if (checkboxInputs.length > 0 || itemEl.querySelector('input[type="checkbox"]')) {
         var selectedOptions = collectMultipleChoiceValue(itemEl);
         if (selectedOptions.length > 0) {
           responses[itemId] = {option_keys: selectedOptions};
         }
         continue;
       }
  
       // Check if this item has a text input
       var textInput = itemEl.querySelector('input.qa-text-input, textarea.qa-text-input');
       if (textInput) {
         var val = textInput.value.trim();
         if (val) {
           responses[itemId] = {text: val};
         }
         continue;
       }
 
       // Check if this item has a radio group (likert or choice)
       var checkedRadio = itemEl.querySelector('input[type="radio"]:checked');
       if (checkedRadio) {
         var name = checkedRadio.name;
         var radios = document.querySelectorAll('input[name="' + name + '"]');
         if (radios.length > 0) {
           // Likert scale: use numeric value
           var numVal = parseInt(checkedRadio.value, 10);
           if (!isNaN(numVal) && numVal >= 1 && numVal <= 10) {
             responses[itemId] = numVal;
           } else {
             // Single choice: use option_key
             responses[itemId] = {option_key: checkedRadio.value};
           }
         }
       }
     }
 
     // Matrix rows
     var matrixRows = document.querySelectorAll('tr[data-item-id]');
     for (var mi = 0; mi < matrixRows.length; mi++) {
       var row = matrixRows[mi];
       var itemId2 = parseInt(row.dataset.itemId, 10);
       if (!itemId2) continue;
       var checked2 = row.querySelector('input[type="radio"]:checked');
       if (checked2) {
         responses[itemId2] = parseInt(checked2.value, 10);
       }
     }
 
     return responses;
   }
 
   // ============================================================
   // Submission
   // ============================================================
   function submitQuestionnaire() {
     if (isSubmitting) return;
     var q = findQ(openQuestionnaireId);
     if (!q || !currentStage) return;
 
     // Validate
     var errorContainer = document.getElementById('qaErrorContainer');
     if (errorContainer) errorContainer.innerHTML = '';
 
     var errors = validateAll();
     if (errors.length > 0) {
       if (errorContainer) {
         errorContainer.innerHTML = '<div class="qa-error-msg">还有 ' + errors.length + ' 道必答题未填写，请完成后再提交。</div>';
       }
       return;
     }
 
     // Collect responses
     var responses = collectResponses();
     var btn = document.getElementById('qaSubmitBtn');
     var statusEl = document.getElementById('qaSubmitStatus');
     if (btn) btn.disabled = true;
     if (statusEl) statusEl.textContent = '提交中...';
     isSubmitting = true;
 
     var payload = {
       response_stage: currentStage,
       responses: responses
     };
 
     fetch('/api/student/questionnaires/' + q.id + '/responses', withTabToken({
       method: 'POST',
       headers: {'Content-Type': 'application/json'},
       body: JSON.stringify(payload)
     }))
     .then(function(res) {
       if (res.status === 409) {
         throw new Error('CONFLICT');
       }
       if (!res.ok) {
         return res.json().then(function(data) {
           var msg = data.error || '提交失败';
           if (data.details && data.details.length > 0) {
             var missing = data.details.filter(function(d) { return d.error === 'required field missing'; });
             if (missing.length > 0) {
               msg = '还有 ' + missing.length + ' 道必答题未填写，请完成后再提交';
               // Highlight first missing
               var firstMissingId = missing[0].item_id;
               var el = document.querySelector('[data-item-id="' + firstMissingId + '"]');
               if (el) {
                 el.classList.add('has-error');
                 el.scrollIntoView({behavior: 'smooth', block: 'center'});
               }
             }
           }
           throw new Error(msg);
         });
       }
       return res.json();
     })
     .then(function(data) {
       // Success
       clearQuestionnaireDraft(q.id, currentStage);
       openQuestionnaireId = null;
       currentStage = null;
       return loadQuestionnaires();
     })
     .catch(function(err) {
       if (err.message === 'CONFLICT') {
         // 409: already submitted - refresh state
         clearQuestionnaireDraft(q.id, currentStage);
         openQuestionnaireId = null;
         currentStage = null;
         return loadQuestionnaires();
       }
       if (errorContainer) {
         errorContainer.innerHTML = '<div class="qa-error-msg">' + escapeHtml(err.message || '提交失败，请重试') + '</div>';
       }
     })
     .finally(function() {
       if (btn) btn.disabled = false;
       if (statusEl) statusEl.textContent = '';
       isSubmitting = false;
     });
   }
 
   // ============================================================
   // Load questionnaires from API
   // ============================================================
   function loadQuestionnaires() {
     return fetchJSON('/api/student/questionnaires?stage=' + encodeURIComponent(mode || 'pre'))
       .then(function(data) {
         questionnaires = data.questionnaires || [];
         questionnaireStatus = data.status || 'ok';
         questionnaireMessage = data.message || '';
         currentSessionId = data.session && data.session.session_id ? String(data.session.session_id) : '';
         postCheckinCompleted = !!data.post_checkin_completed;
         if (mode === 'post' && questionnaireStatus !== 'waiting_session') {
           questionnaires = [{
             id: POST_CHECKIN_ID,
             title: '讨论后情绪打卡',
             description: '记录刚才讨论后的主观体验',
             allowed_stages: ['post'],
             completed_stages: postCheckinCompleted ? ['post'] : [],
             is_checkin: true
           }].concat(questionnaires);
         }
         for (var i = 0; i < questionnaires.length; i++) {
           if (!isPostCheckin(questionnaires[i]) && isCompleted(questionnaires[i])) {
             clearQuestionnaireDraft(questionnaires[i].id, mode);
           }
         }
         if (openQuestionnaireId && !findQ(openQuestionnaireId)) {
           openQuestionnaireId = null;
         }
         renderQuestionnaireList();
       })
       .catch(function(err) {
         var summary = document.getElementById('questionnaireSummary');
         if (summary) {
           summary.innerHTML = '<div class="qa-error-msg">加载问卷失败: ' + escapeHtml(err.message || '未知错误') + '</div>';
         }
       });
   }
 
   // ============================================================
   // Init
   // ============================================================
   function init(stage, token, stageLabel) {
     mode = stage || 'pre';
     modeLabel = stageLabel || getStageLabel(mode);
     tabToken = token || '';
     currentStage = mode;
     var title = document.querySelector('.qa-header h2');
     if (title) title.textContent = mode === 'pre' ? '研究前测问卷' : '研究后测问卷';
     var refreshBtn = document.querySelector('.qa-header .chat-action-btn');
     if (refreshBtn) refreshBtn.textContent = '刷新';
 
     document.getElementById('questionnaireSummary').innerHTML = '<div class="qa-info-msg">正在读取问卷...</div>';
 
     loadQuestionnaires();
 
     // Click handler for "返回讨论" buttons (delegated)
     document.addEventListener('click', function(e) {
       var target = e.target;
       if (target.classList.contains('qa-list-btn')) {
         var qid = parseInt(target.dataset.qid, 10);
         if (qid) openQuestionnaire(qid);
       }
     });
   }
 
   // ============================================================
   // Reload / Refresh
   // ============================================================
   function reload() {
     openQuestionnaireId = null;
     currentStage = null;
     var summary = document.getElementById('questionnaireSummary');
     if (summary) {
      summary.innerHTML = '<div class="qa-info-msg">' + getStageLabel(mode) + '正在加载...</div>';
     }
     loadQuestionnaires();
   }

   // ============================================================
   // Public API
   // ============================================================
   return {
     init: init,
     loadQuestionnaires: loadQuestionnaires,
     submitQuestionnaire: submitQuestionnaire,
     submitPostEmotionCheckin: submitPostEmotionCheckin,
     reload: reload,
     openQuestionnaire: openQuestionnaire,
     // Exposed for testing
     _renderers: {
       textQuestionHTML: textQuestionHTML,
       singleChoiceHTML: singleChoiceHTML,
       likertHTML: likertHTML,
       matrixLikertSectionHTML: matrixLikertSectionHTML
     },
     _collect: {
       collectResponses: collectResponses,
       collectTextValue: collectTextValue,
       collectSingleChoiceValue: collectSingleChoiceValue,
       collectLikertValue: collectLikertValue
     },
     _validate: {
       validateAll: validateAll,
       validateText: validateText,
       validateSingleChoice: validateSingleChoice,
       validateLikert: validateLikert
     }
   };
 })();
