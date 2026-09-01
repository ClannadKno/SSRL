const { routeForScenario } = require('./strategyRouteManifest');

const SUCCESS_UNCOVERED = 'assessment_complete_unconfirmed';
const UNKNOWN_SUB_STATE = 'unknown_sub_state';
const FAILED_UNCLASSIFIED = 'assessment_failed_unclassified';
const UNCLASSIFIED_DISPLAY_CODES = new Set([
  SUCCESS_UNCOVERED,
  UNKNOWN_SUB_STATE,
  FAILED_UNCLASSIFIED
]);
const TERMINAL_BATCH_STATUSES = new Set([
  'succeeded',
  'failed',
  'degraded',
  'quarantined'
]);
const TERMINAL_PIPELINE_STATUSES = new Set([
  'PUBLISHED',
  'SUPPRESSED',
  'STALE',
  'SUPERSEDED',
  'FAILED',
  'SKIPPED'
]);

function buildBatch18LabelAudit({
  sessionId,
  sourceSummary,
  session,
  groups,
  ui,
  manifest
}) {
  const normalizedGroups = Array.isArray(groups) ? groups : [];
  const apiMessages = [];
  const exportMessages = [];
  const unifiedMessages = [];
  const batches = [];
  const pipelines = [];
  const latencyEvents = [];
  const consistencyMismatches = [];
  const summaryMismatches = [];

  for (const group of normalizedGroups) {
    const review = group.review || {};
    const studentApiMessages = studentRows(review.messages);
    const studentExportMessages = studentRows(group.messagesExport);
    const studentUnifiedMessages = (group.unifiedExport || []).filter((row) => (
      String(row.actor_role || '').toLowerCase() === 'student' &&
      String(row.event_type || '').toLowerCase() === 'state_assignment'
    ));
    apiMessages.push(...studentApiMessages.map((row) => withGroup(row, group)));
    exportMessages.push(...studentExportMessages.map((row) => withGroup(row, group)));
    unifiedMessages.push(...studentUnifiedMessages.map((row) => withGroup(row, group)));

    compareMessageViews({
      group,
      apiMessages: studentApiMessages,
      exportMessages: studentExportMessages,
      unifiedMessages: studentUnifiedMessages,
      mismatches: consistencyMismatches
    });
    compareAssignmentSummaries(group, summaryMismatches);

    const tables = ((group.dbAudit || {}).tables) || {};
    batches.push(...(tables.state_assessment_batches || []));
    pipelines.push(...(tables.strategy_pipeline_runs || []));
    latencyEvents.push(...(tables.strategy_pipeline_latency_events || []));
  }

  const categoryCounts = countDisplayCategories(apiMessages);
  const displayMissing = apiMessages.filter((message) => (
    !nonEmpty(message.display_state_code) || !nonEmpty(message.display_state_label)
  ));
  const improperBackfills = apiMessages.filter((message) => (
    UNCLASSIFIED_DISPLAY_CODES.has(String(message.display_state_code || '')) &&
    nonEmpty(message.final_sub_state_code)
  ));
  const displayCoverageRate = categoryCounts.student_message_count
    ? categoryCounts.display_assigned_student_message_count / categoryCounts.student_message_count
    : 0;

  const batchHealth = summarizeBatchHealth(batches);
  const pipelineHealth = summarizePipelineHealth(pipelines);
  const stage2Health = summarizeStage2Health(latencyEvents);
  const strategyCompatibility = summarizeStrategyCompatibility(pipelines, manifest);
  const uiConsistency = summarizeUiConsistency(ui, normalizedGroups);
  const sourceRun = summarizeSourceRun(sourceSummary, sessionId);
  const sessionMatches = Number(session && session.id) === Number(sessionId);
  const agentFlagsMatch = Boolean(
    session &&
    Number(session.agent_detection_enabled) === 1 &&
    Number(session.strategy_agent_enabled) === 1 &&
    Number(session.emotion_agent_enabled) === 0
  );

  const checks = {
    real_chain_source_verified: sourceRun.passed,
    session_scope_verified: sessionMatches,
    agent_flags_verified: agentFlagsMatch,
    all_six_groups_audited: normalizedGroups.length === 6,
    display_label_coverage_100_percent: displayCoverageRate === 1 && displayMissing.length === 0,
    success_uncovered_reproduced: categoryCounts.success_uncovered_student_message_count > 0,
    unknown_sub_state_reproduced: categoryCounts.unknown_sub_state_student_message_count > 0,
    failed_window_reproduced: categoryCounts.failed_unclassified_student_message_count > 0,
    no_unclassified_state_backfill: improperBackfills.length === 0,
    teacher_api_export_consistent: consistencyMismatches.length === 0,
    teacher_api_summaries_consistent: summaryMismatches.length === 0,
    teacher_ui_api_consistent: uiConsistency.passed,
    stage2_no_truncation_or_reasoning_exhaustion: stage2Health.passed,
    all_batches_terminal: batchHealth.passed,
    all_pipelines_terminal_and_stage3_healthy: pipelineHealth.passed,
    selected_strategy_runtime_route_compatible: strategyCompatibility.passed
  };

  return {
    schema_version: 'batch18-label-audit/1',
    generated_at: new Date().toISOString(),
    session_id: Number(sessionId),
    source_run: sourceRun,
    session: {
      id: session && session.id,
      session_no: session && session.session_no,
      status: session && session.status,
      strategy_agent_enabled: session && session.strategy_agent_enabled,
      emotion_agent_enabled: session && session.emotion_agent_enabled
    },
    counts: {
      ...categoryCounts,
      group_count: normalizedGroups.length,
      api_export_mismatch_count: consistencyMismatches.length,
      api_summary_mismatch_count: summaryMismatches.length,
      improper_backfill_count: improperBackfills.length
    },
    display_label_coverage_rate: roundRate(displayCoverageRate),
    batch_health: batchHealth,
    pipeline_health: pipelineHealth,
    stage2_health: stage2Health,
    strategy_compatibility: strategyCompatibility,
    teacher_ui: uiConsistency,
    strict_scenario_trigger_coverage: {
      acceptance_role: 'diagnostic_only',
      affects_batch18_pass: false,
      passed_case_count: sourceRun.strict_scenario_passed_count,
      expected_case_count: sourceRun.strict_scenario_expected_count
    },
    mismatches: {
      api_export: consistencyMismatches.slice(0, 50),
      api_summary: summaryMismatches.slice(0, 50),
      improper_backfill_message_ids: improperBackfills.slice(0, 50).map(messageIdentity),
      missing_display_message_ids: displayMissing.slice(0, 50).map(messageIdentity)
    },
    checks,
    passed: Object.values(checks).every(Boolean)
  };
}

function countDisplayCategories(messages) {
  const counts = {
    student_message_count: messages.length,
    display_assigned_student_message_count: 0,
    precise_state_student_message_count: 0,
    unknown_sub_state_student_message_count: 0,
    success_uncovered_student_message_count: 0,
    failed_unclassified_student_message_count: 0,
    other_unclassified_student_message_count: 0
  };
  for (const message of messages) {
    const code = String(message.display_state_code || '');
    if (nonEmpty(code) && nonEmpty(message.display_state_label)) {
      counts.display_assigned_student_message_count += 1;
    }
    if (code === UNKNOWN_SUB_STATE) counts.unknown_sub_state_student_message_count += 1;
    else if (code === SUCCESS_UNCOVERED) counts.success_uncovered_student_message_count += 1;
    else if (code === FAILED_UNCLASSIFIED) counts.failed_unclassified_student_message_count += 1;
    else if (nonEmpty(message.final_sub_state_code)) counts.precise_state_student_message_count += 1;
    else counts.other_unclassified_student_message_count += 1;
  }
  return counts;
}

function compareMessageViews({ group, apiMessages, exportMessages, unifiedMessages, mismatches }) {
  const exported = new Map(exportMessages.map((row) => [String(row.message_id || row.id), row]));
  const unified = new Map(unifiedMessages.map((row) => [String(row.related_id || row.event_id), row]));
  for (const message of apiMessages) {
    const id = String(message.id || message.message_id);
    compareMessage(group, id, 'messages.csv', message, exported.get(id), mismatches);
    compareMessage(group, id, 'unified-events.csv', message, unified.get(id), mismatches);
  }
  if (exportMessages.length !== apiMessages.length) {
    mismatches.push(countMismatch(group, 'messages.csv', apiMessages.length, exportMessages.length));
  }
  if (unifiedMessages.length !== apiMessages.length) {
    mismatches.push(countMismatch(group, 'unified-events.csv', apiMessages.length, unifiedMessages.length));
  }
}

function compareMessage(group, id, source, api, other, mismatches) {
  if (!other) {
    mismatches.push({ group_code: group.groupCode, message_id: id, source, reason: 'missing_row' });
    return;
  }
  for (const field of ['display_state_code', 'display_state_label', 'final_sub_state_code']) {
    if (normalize(api[field]) !== normalize(other[field])) {
      mismatches.push({
        group_code: group.groupCode,
        message_id: id,
        source,
        field,
        api_value: normalize(api[field]),
        source_value: normalize(other[field])
      });
    }
  }
}

function compareAssignmentSummaries(group, mismatches) {
  const reviewSummary = (group.review || {}).message_assignment_summary || {};
  const detailSummary = (group.detail || {}).message_assignment_summary || {};
  const fields = [
    'student_message_count',
    'display_assigned_student_message_count',
    'precise_sub_state_message_count',
    'unknown_sub_state_student_message_count',
    'success_uncovered_student_message_count',
    'failed_unclassified_student_message_count'
  ];
  for (const field of fields) {
    if (Number(reviewSummary[field] || 0) !== Number(detailSummary[field] || 0)) {
      mismatches.push({
        group_code: group.groupCode,
        field,
        review_value: Number(reviewSummary[field] || 0),
        detail_value: Number(detailSummary[field] || 0)
      });
    }
  }
  if (reviewSummary.policy !== 'message_state_assignment_v2') {
    mismatches.push({ group_code: group.groupCode, field: 'policy', review_value: reviewSummary.policy || null });
  }
}

function summarizeBatchHealth(rows) {
  const uniqueRows = uniqueById(rows);
  const nonterminal = uniqueRows.filter((row) => !TERMINAL_BATCH_STATUSES.has(String(row.status || '').toLowerCase()));
  const statusCounts = countBy(uniqueRows, (row) => String(row.status || 'missing').toLowerCase());
  return {
    batch_count: uniqueRows.length,
    status_counts: statusCounts,
    nonterminal_batch_ids: nonterminal.slice(0, 50).map((row) => row.id),
    duplicate_row_count: rows.length - uniqueRows.length,
    passed: uniqueRows.length > 0 && nonterminal.length === 0 && rows.length === uniqueRows.length
  };
}

function summarizePipelineHealth(rows) {
  const uniqueRows = uniqueById(rows);
  const nonterminal = uniqueRows.filter((row) => !TERMINAL_PIPELINE_STATUSES.has(String(row.final_status || '').toUpperCase()));
  const stage3Failed = uniqueRows.filter((row) => String(row.stage3_status || '').toUpperCase() === 'FAILED');
  return {
    pipeline_count: uniqueRows.length,
    final_status_counts: countBy(uniqueRows, (row) => String(row.final_status || 'missing').toUpperCase()),
    nonterminal_pipeline_ids: nonterminal.slice(0, 50).map((row) => row.id),
    stage3_failed_pipeline_ids: stage3Failed.slice(0, 50).map((row) => row.id),
    duplicate_row_count: rows.length - uniqueRows.length,
    passed: uniqueRows.length > 0 && nonterminal.length === 0 && stage3Failed.length === 0 && rows.length === uniqueRows.length
  };
}

function summarizeStage2Health(events) {
  const attempts = uniqueById(events.filter((event) => (
    /^stage2_(?:llm_attempt_\d+|repair)_finished$/.test(String(event.event || ''))
  )));
  const truncated = [];
  const exhausted = [];
  const nonJson = [];
  const finishReasons = {};
  for (const event of attempts) {
    const details = objectValue(event.details_json);
    const finishReason = String(details.finish_reason || 'missing');
    finishReasons[finishReason] = (finishReasons[finishReason] || 0) + 1;
    const failureText = [details.failure_category, details.failure_type, details.error]
      .filter(Boolean).join(' ').toLowerCase();
    if (
      details.incomplete_response === true ||
      details.response_incomplete === true ||
      finishReason.toLowerCase() === 'length' ||
      failureText.includes('response_truncated')
    ) truncated.push(event.id);
    if (failureText.includes('reasoning_budget_exhausted')) exhausted.push(event.id);
    if (details.json_extractable !== true) nonJson.push(event.id);
  }
  return {
    external_attempt_count: attempts.length,
    finish_reason_counts: finishReasons,
    truncated_attempt_ids: truncated.slice(0, 50),
    reasoning_budget_exhausted_attempt_ids: exhausted.slice(0, 50),
    non_json_extractable_attempt_ids: nonJson.slice(0, 50),
    passed: attempts.length > 0 && truncated.length === 0 && exhausted.length === 0 && nonJson.length === 0
  };
}

function summarizeStrategyCompatibility(rows, manifest) {
  const selected = uniqueById(rows).filter((row) => nonEmpty(row.selected_strategy_id));
  const results = selected.map((row) => {
    const primaryState = String(row.canonical_sub_state_code || '').trim();
    const overlays = arrayValue(row.state_overlays || row.secondary_sub_state_tags_json);
    const route = routeForScenario({
      canonicalSubState: primaryState,
      expectedPrimaryState: primaryState,
      expectedOverlayTags: overlays
    }, manifest);
    const strategyId = String(row.selected_strategy_id || '').trim();
    return {
      pipeline_run_id: row.id,
      primary_state: primaryState,
      overlay_states: overlays,
      selected_strategy_id: strategyId,
      runtime_allowed_strategy_ids: route.runtimeAllowedStrategyIds,
      route_source_version: route.routeSourceVersion,
      route_valid: route.runtimeAllowedStrategyIds.includes(strategyId)
    };
  });
  const invalid = results.filter((item) => !item.route_valid);
  return {
    selected_strategy_count: results.length,
    compatible_strategy_count: results.length - invalid.length,
    compatibility_rate: results.length ? roundRate((results.length - invalid.length) / results.length) : 0,
    invalid_selections: invalid.slice(0, 50),
    selections: results,
    passed: results.length > 0 && invalid.length === 0
  };
}

function summarizeUiConsistency(ui, groups) {
  const target = groups.find((group) => String(group.groupCode) === String(ui && ui.groupCode));
  if (!target) return { passed: false, reason: 'ui_group_missing' };
  const apiStudents = studentRows((target.review || {}).messages);
  const apiBySequence = new Map(apiStudents.map((row) => [String(row.sequence), row]));
  const uiStudents = (ui.messages || []).filter((row) => apiBySequence.has(String(row.sequence)));
  const mismatches = [];
  for (const row of uiStudents) {
    const api = apiBySequence.get(String(row.sequence));
    if (normalize(api.display_state_label) !== normalize(row.display_state_label)) {
      mismatches.push({
        sequence: row.sequence,
        api_label: normalize(api.display_state_label),
        ui_label: normalize(row.display_state_label)
      });
    }
  }
  const codes = new Set(apiStudents.map((row) => String(row.display_state_code || '')));
  return {
    group_code: ui.groupCode,
    api_student_message_count: apiStudents.length,
    ui_student_message_count: uiStudents.length,
    mismatch_count: mismatches.length,
    mismatches: mismatches.slice(0, 50),
    required_categories_visible: {
      success_uncovered: codes.has(SUCCESS_UNCOVERED),
      unknown_sub_state: codes.has(UNKNOWN_SUB_STATE),
      failed_window: codes.has(FAILED_UNCLASSIFIED)
    },
    passed: apiStudents.length > 0 && uiStudents.length === apiStudents.length &&
      mismatches.length === 0 &&
      codes.has(SUCCESS_UNCOVERED) && codes.has(UNKNOWN_SUB_STATE) && codes.has(FAILED_UNCLASSIFIED)
  };
}

function summarizeSourceRun(summary, sessionId) {
  const source = summary || {};
  const integrity = source.input_integrity || {};
  const stats = source.coverage_statistics || source.coverageStatistics || {};
  const expectedMessages = Number(
    integrity.expected_script_messages || stats.expected_script_messages || 0
  );
  const successfulMessages = Number(
    integrity.successful_script_messages || stats.successful_script_messages || 0
  );
  const auditedSessionId = Number(
    (source.actualStrategyCoverage || {}).sessionId ||
    (source.canonical_db_coverage || {}).session_id ||
    sessionId
  );
  return {
    run_id: source.runId || null,
    scenario: source.scenario || null,
    validation_mode: source.validationMode || null,
    audit_available: Boolean(source.auditAvailable),
    session_id: auditedSessionId,
    expected_script_messages: expectedMessages,
    successful_script_messages: successfulMessages,
    input_status: integrity.status || integrity.final_status || null,
    strict_scenario_passed_count: Number(stats.scenario_coverage_passed_count || 0),
    strict_scenario_expected_count: Number(stats.scenario_coverage_expected_count || 0),
    passed: Boolean(
      source.runId &&
      source.validationMode === 'real_coverage' &&
      source.auditAvailable === true &&
      auditedSessionId === Number(sessionId) &&
      expectedMessages > 0 &&
      successfulMessages === expectedMessages &&
      String(integrity.status || integrity.final_status || '').toUpperCase() === 'COMPLETE'
    )
  };
}

function studentRows(rows) {
  return (rows || []).filter((row) => (
    String(row.role || row.sender_type || '').toLowerCase() === 'student'
  ));
}

function uniqueById(rows) {
  return [...new Map((rows || []).map((row, index) => [String(row.id ?? `index:${index}`), row])).values()];
}

function countBy(rows, keyFn) {
  return rows.reduce((acc, row) => {
    const key = keyFn(row);
    acc[key] = (acc[key] || 0) + 1;
    return acc;
  }, {});
}

function countMismatch(group, source, expected, actual) {
  return { group_code: group.groupCode, source, reason: 'row_count', expected, actual };
}

function withGroup(row, group) {
  return { ...row, __group_code: group.groupCode };
}

function messageIdentity(message) {
  return {
    group_code: message.__group_code || null,
    message_id: message.id || message.message_id || null,
    sequence: message.sequence || null
  };
}

function nonEmpty(value) {
  return value !== null && value !== undefined && String(value).trim() !== '';
}

function normalize(value) {
  return value === null || value === undefined ? '' : String(value).trim();
}

function objectValue(value) {
  if (value && typeof value === 'object' && !Array.isArray(value)) return value;
  try {
    const parsed = JSON.parse(String(value || '{}'));
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch (_error) {
    return {};
  }
}

function arrayValue(value) {
  if (Array.isArray(value)) return value.map(String);
  try {
    const parsed = JSON.parse(String(value || '[]'));
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch (_error) {
    return [];
  }
}

function roundRate(value) {
  return Math.round(Number(value || 0) * 10000) / 10000;
}

module.exports = {
  FAILED_UNCLASSIFIED,
  SUCCESS_UNCOVERED,
  UNKNOWN_SUB_STATE,
  buildBatch18LabelAudit,
  countDisplayCategories,
  summarizeStage2Health,
  summarizeStrategyCompatibility
};
