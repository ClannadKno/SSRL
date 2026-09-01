const {
  buildActualStrategyCoverage,
  scenarioScopedPipelineRuns,
  scriptedScenarios,
} = require('./strategyCoverage');
const {
  SCENARIO_TYPES,
  classifyScenario,
  expectedOverlayStates,
  expectedPrimaryState,
  scenarioTypeFor,
  scenariosByType
} = require('./scenarioClassification');
const {
  isStrategyAllowedForScenario,
  routeForScenario
} = require('./strategyRouteManifest');

const LEGACY_STATE_CODES = new Set([
  'positive_collaboration',
  'negative_silence',
  'conflict_tension',
  'blocked_frustration',
  'task_detached',
  'unknown'
]);

const TERMINAL_PIPELINE_STATUSES = new Set([
  'PUBLISHED',
  'SUPPRESSED',
  'COMPLETED',
  'SKIPPED',
  'FAILED',
  'PASS',
  'CANCELLED',
  'SUPERSEDED',
  'STALE',
  'EXPIRED',
  'SUPPRESSED_COOLDOWN',
  'SUPPRESSED_SESSION_CLOSED',
  'SUPPRESSED_AGENT_DISABLED',
  'ALREADY_PUBLISHED',
  'NOT_PUBLISHED'
]);

function unique(items) {
  return [...new Set((items || []).filter(Boolean).map((item) => String(item)))];
}

function upper(value) {
  return String(value || '').trim().toUpperCase();
}

function statRowIdentity(row, tableName) {
  if (tableName === 'messages') {
    return String(row && (row.message_id || row.id || ''));
  }
  if (tableName === 'intervention_runs') {
    return String(row && (row.intervention_run_id || row.id || ''));
  }
  return String(row && (row.pipeline_run_id || row.id || row.run_uuid || ''));
}

function auditKeyForTable(tableName) {
  return {
    messages: 'message_timeline',
    strategy_pipeline_runs: 'strategy_pipeline_runs',
    intervention_runs: 'interventions'
  }[tableName];
}

function snapshotRowsForStats(group, tableName) {
  const tables = group && group.dbAudit && group.dbAudit.tables;
  if (tables && Array.isArray(tables[tableName])) return tables[tableName];
  const auditKey = auditKeyForTable(tableName);
  return auditKey && group && group.audit && Array.isArray(group.audit[auditKey])
    ? group.audit[auditKey]
    : [];
}

function newStatsRows(baselineGroup, finalGroup, tableName) {
  const baselineRows = snapshotRowsForStats(baselineGroup, tableName);
  const finalRows = snapshotRowsForStats(finalGroup, tableName);
  const baselineIds = new Set(baselineRows.map((row) => statRowIdentity(row, tableName)));
  return finalRows
    .filter((row) => !baselineIds.has(statRowIdentity(row, tableName)))
    .map((row) => ({
      ...row,
      __groupCode: finalGroup && finalGroup.groupCode
    }));
}

function newStatsRowsAcrossGroups(baselineSnapshot, finalSnapshot, tableName) {
  const baselineByGroup = snapshotGroupsByCode(baselineSnapshot);
  return ((finalSnapshot && finalSnapshot.groups) || []).flatMap((finalGroup) => (
    newStatsRows(baselineByGroup.get(finalGroup.groupCode), finalGroup, tableName)
  ));
}

function statPipelineId(row) {
  return statRowIdentity(row, 'strategy_pipeline_runs');
}

function hasValue(value) {
  return value !== undefined && value !== null && String(value) !== '';
}

function statBoolean(value) {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  const normalized = String(value || '').trim().toLowerCase();
  if (['true', '1', 'yes', '是'].includes(normalized)) return true;
  if (['false', '0', 'no', '否'].includes(normalized)) return false;
  return null;
}

function statStage3Started(row) {
  return hasValue(row && row.stage3_started_at) || hasValue(row && row.stage3_completed_at) ||
    ['RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'].includes(upper(row && row.stage3_status));
}

function statStage3Succeeded(row) {
  return upper(row && row.stage3_status) === 'SUCCEEDED';
}

function statStage3Failed(row) {
  return ['FAILED', 'ERROR', 'FAILURE', 'TIMEOUT'].includes(upper(row && row.stage3_status));
}

function statPublishAttempted(row) {
  const status = upper(row && row.publish_status);
  return hasValue(row && row.published_message_id) ||
    Boolean(status && !['NOT_READY', 'PENDING', 'RUNNING', 'SKIPPED'].includes(status));
}

function statPublished(row) {
  return upper(row && row.publish_status) === 'PUBLISHED' ||
    upper(row && row.final_status) === 'PUBLISHED' ||
    hasValue(row && row.published_message_id);
}

function countUniquePipelineRows(rows, predicate) {
  return new Set(rows.filter(predicate).map(statPipelineId)).size;
}

function statCoveragePassedCount(coverage) {
  if (!coverage) return 0;
  if (Number.isFinite(Number(coverage.passedCaseCount))) {
    return Number(coverage.passedCaseCount);
  }
  return (coverage.scenarios || []).filter((item) => item.passed).length;
}

function statCoverageExpectedCount(coverage, fallback) {
  if (!coverage) return fallback;
  if (Number.isFinite(Number(coverage.expectedCaseCount))) {
    return Number(coverage.expectedCaseCount);
  }
  return Array.isArray(coverage.scenarios) ? coverage.scenarios.length : fallback;
}

function pipelineOutcomeValues(row) {
  return unique([
    ...(pipelineStatuses(row) || []),
    row && row.terminal_reason,
    row && row.suppression_reason,
    row && row.preflight_gate_reason,
    row && row.replacement_reason
  ].map(upper));
}

function pipelineHasOutcome(row, matcher) {
  return pipelineOutcomeValues(row).some((value) => matcher(value));
}

function countCoverageScenarioResults(coverage, scenarioIds, field = 'passed') {
  const ids = new Set((scenarioIds || []).map((value) => String(value)));
  return (coverage && coverage.scenarios || []).filter((item) => (
    ids.has(String(item.scenarioId || item.scenario_id)) &&
    (item[field] === undefined ? item.passed : item[field])
  )).length;
}

function buildCoverageStatistics(
  scenario,
  baselineSnapshot,
  finalSnapshot,
  events = [],
  {
    interventionCoverage = null,
    inhibitionCoverage = null,
    actualStateCoverage = null,
    optionalSupportCoverage = null,
    inputIntegrity = null
  } = {}
) {
  const scenarios = scriptedScenarios(scenario);
  const expectedInterventionCases = scenariosByType(
    scenarios.filter((item) => !item.expectedFailureFallback),
    SCENARIO_TYPES.REQUIRED_INTERVENTION
  );
  const optionalSupportCases = scenariosByType(
    scenarios.filter((item) => !item.expectedFailureFallback),
    SCENARIO_TYPES.OPTIONAL_SUPPORT
  );
  const observationInhibitionCases = scenariosByType(
    scenarios.filter((item) => !item.expectedFailureFallback),
    SCENARIO_TYPES.OBSERVATION_INHIBITION
  );
  const expectedScenarioIds = new Set(expectedInterventionCases.map((item) => item.id));
  const pipelines = newStatsRowsAcrossGroups(
    baselineSnapshot,
    finalSnapshot,
    'strategy_pipeline_runs'
  );
  const interventions = newStatsRowsAcrossGroups(
    baselineSnapshot,
    finalSnapshot,
    'intervention_runs'
  );
  const messages = newStatsRowsAcrossGroups(
    baselineSnapshot,
    finalSnapshot,
    'messages'
  );
  const latencyEvents = newDbItemsAcrossGroups(
    baselineSnapshot,
    finalSnapshot,
    'strategy_pipeline_latency_events'
  );
  const publishStartedEvents = latencyEvents.filter((event) => event.event === 'publish_started');
  const publishGateEvents = latencyEvents.filter((event) => event.event === 'publish_gate_evaluated');
  const messageCommittedEvents = latencyEvents.filter((event) => event.event === 'message_committed');
  const publishRetryOrDuplicateEvents = latencyEvents.filter((event) => (
    ['pipeline_duplicate', 'publish_retry', 'publish_duplicate'].includes(event.event) ||
    String(event.event || '').toLowerCase().includes('duplicate') ||
    String(event.event || '').toLowerCase().includes('retry')
  ));
  const stage3SuccessPipelineIds = new Set(
    pipelines.filter(statStage3Succeeded).map(statPipelineId)
  );
  const publishAttemptPipelineIds = new Set(
    publishStartedEvents
      .map((event) => String(event.pipeline_run_id || ''))
      .filter((pipelineId) => pipelineId && (
        !stage3SuccessPipelineIds.size || stage3SuccessPipelineIds.has(pipelineId)
      ))
  );
  const publishGatePassedPipelineIds = new Set(
    publishGateEvents
      .filter((event) => {
        const details = typeof event.details_json === 'string'
          ? (() => { try { return JSON.parse(event.details_json); } catch (_error) { return {}; } })()
          : (event.details_json || {});
        return details.publish_gate_allowed === true ||
          details.publish_gate_result === 'allowed';
      })
      .map((event) => String(event.pipeline_run_id || ''))
      .filter(Boolean)
  );
  const committedPipelineIds = new Set(
    messageCommittedEvents
      .map((event) => String(event.pipeline_run_id || ''))
      .filter(Boolean)
  );
  const hasPublishTelemetry = publishStartedEvents.length > 0 ||
    publishGateEvents.length > 0 || messageCommittedEvents.length > 0;
  const publishedPipelines = pipelines.filter(statPublished);
  const publishedPipelineIds = new Set(publishedPipelines.map(statPipelineId));
  const publishedMessageIds = new Set(
    publishedPipelines
      .map((row) => row.published_message_id)
      .filter(hasValue)
      .map(String)
  );
  const publishedInterventionIds = new Set(
    interventions
      .filter((row) => publishedPipelineIds.has(String(row.strategy_pipeline_run_id)))
      .map((row) => row.intervention_run_id || row.id)
      .filter(hasValue)
      .map(String)
  );
  for (const row of interventions) {
    if (!publishedPipelineIds.has(String(row.strategy_pipeline_run_id))) continue;
    if (hasValue(row.message_id)) publishedMessageIds.add(String(row.message_id));
  }
  const visibleAgentMessages = messages.filter((row) => {
    const role = upper(row.role || row.sender_type);
    if (role !== 'AGENT') return false;
    return publishedMessageIds.has(String(row.message_id || row.id)) ||
      publishedPipelineIds.has(String(row.strategy_pipeline_run_id || row.pipeline_run_id)) ||
      publishedInterventionIds.has(String(row.intervention_run_id));
  });
  const completedBoundaryEvents = [];
  const boundaryKeys = new Set();
  for (const event of events || []) {
    if (event.type !== 'expected_intervention_completed') continue;
    const scenarioId = event.scenarioId || event.scenario_id;
    const triggerMessageId = event.messageId || event.triggerMessageId || event.trigger_message_id;
    if (!scenarioId || !expectedScenarioIds.has(scenarioId) || !hasValue(triggerMessageId)) continue;
    const key = `${scenarioId}:${triggerMessageId}`;
    if (boundaryKeys.has(key)) continue;
    boundaryKeys.add(key);
    completedBoundaryEvents.push({
      scenario_id: scenarioId,
      trigger_message_id: triggerMessageId,
      published_message_id: event.agentMessageId || event.agent_message_id || null,
      pipeline_run_id: event.pipelineRunId || event.pipeline_run_id || null,
      group_id: event.groupId || event.group_id || null,
      discussion_id: event.discussionId || event.discussion_id || null,
      session_id: event.sessionId || event.session_id || null
    });
  }
  const requiredScenarioIds = expectedInterventionCases.map((item) => item.id);
  const optionalScenarioIds = optionalSupportCases.map((item) => item.id);
  const requiredInterventionStatePassed = countCoverageScenarioResults(
    actualStateCoverage,
    requiredScenarioIds,
    'primaryStatePassed'
  ) || countCoverageScenarioResults(actualStateCoverage, requiredScenarioIds, 'passed');
  const requiredInterventionRoutePassed = countCoverageScenarioResults(
    interventionCoverage,
    requiredScenarioIds,
    'routePassed'
  ) || countCoverageScenarioResults(interventionCoverage, requiredScenarioIds, 'passed');
  const requiredInterventionPublished = countCoverageScenarioResults(
    interventionCoverage,
    requiredScenarioIds,
    'publishedPassed'
  ) || countCoverageScenarioResults(interventionCoverage, requiredScenarioIds, 'passed');
  const optionalSupportStatePassed = countCoverageScenarioResults(
    optionalSupportCoverage || actualStateCoverage,
    optionalScenarioIds,
    optionalSupportCoverage ? 'statePassed' : 'primaryStatePassed'
  ) || countCoverageScenarioResults(
    optionalSupportCoverage || actualStateCoverage,
    optionalScenarioIds,
    'passed'
  );
  const overlayScenarioIds = scenarios
    .filter((item) => expectedOverlayStates(item).length > 0)
    .map((item) => item.id);
  const overlayPassed = countCoverageScenarioResults(
    actualStateCoverage,
    overlayScenarioIds,
    'overlayPassed'
  ) || countCoverageScenarioResults(actualStateCoverage, overlayScenarioIds, 'passed');
  const canonicalStates = unique(scenarios
    .filter((item) => !item.expectedFailureFallback)
    .map(expectedPrimaryState));
  const observedCanonicalStates = unique((actualStateCoverage && actualStateCoverage.scenarios || [])
    .filter((item) => item.primaryStatePassed || item.passed)
    .map((item) => item.expectedPrimaryState || item.expectedSubState));
  const cooldownPipelines = pipelines.filter((row) => pipelineHasOutcome(
    row,
    (value) => value.includes('COOLDOWN') || value.includes('COOL_DOWN')
  ));
  const stalePipelines = pipelines.filter((row) => pipelineHasOutcome(row, (value) => value === 'STALE'));
  const supersededPipelines = pipelines.filter(
    (row) => pipelineHasOutcome(row, (value) => value === 'SUPERSEDED')
  );
  const messageInputIntegrity = inputIntegrity || buildScriptedMessageInputIntegrity(scenario, events);
  const messageComplete = messageInputIntegrity.complete;
  const counts = {
    expected_intervention_cases: expectedInterventionCases.length,
    required_intervention_case_count: expectedInterventionCases.length,
    optional_support_case_count: optionalSupportCases.length,
    observation_inhibition_case_count: observationInhibitionCases.length,
    canonical_state_coverage: {
      expected_state_count: canonicalStates.length,
      observed_state_count: observedCanonicalStates.length,
      expected_states: canonicalStates,
      observed_states: observedCanonicalStates,
      missing_states: canonicalStates.filter((state) => !observedCanonicalStates.includes(state)),
      passed: canonicalStates.every((state) => observedCanonicalStates.includes(state))
    },
    overlay_coverage: {
      expected_case_count: overlayScenarioIds.length,
      passed_case_count: overlayPassed,
      passed: overlayPassed === overlayScenarioIds.length
    },
    required_intervention_state_passed: requiredInterventionStatePassed,
    required_intervention_route_passed: requiredInterventionRoutePassed,
    required_intervention_published: requiredInterventionPublished,
    optional_support_state_passed: optionalSupportStatePassed,
    observation_inhibition_passed: statCoveragePassedCount(inhibitionCoverage),
    cooldown_suppressed: new Set(cooldownPipelines.map(statPipelineId)).size,
    stale_terminal: new Set(stalePipelines.map(statPipelineId)).size,
    superseded_terminal: new Set(supersededPipelines.map(statPipelineId)).size,
    message_complete: messageComplete,
    expected_script_messages: messageInputIntegrity.expected_script_messages,
    successful_script_messages: messageInputIntegrity.successful_script_messages,
    attempted_script_messages: messageInputIntegrity.attempted_script_messages,
    failed_script_messages: messageInputIntegrity.failed_script_messages,
    stage2_should_intervene_count: countUniquePipelineRows(
      pipelines,
      (row) => statBoolean(row.should_intervene) === true
    ),
    stage3_started_count: countUniquePipelineRows(pipelines, statStage3Started),
    stage3_succeeded_count: countUniquePipelineRows(pipelines, statStage3Succeeded),
    stage3_failed_count: countUniquePipelineRows(pipelines, statStage3Failed),
    publish_attempted_count: hasPublishTelemetry
      ? publishAttemptPipelineIds.size
      : countUniquePipelineRows(pipelines, statPublishAttempted),
    stage3_success_pipeline_count: stage3SuccessPipelineIds.size,
    publish_attempt_event_count: publishStartedEvents.length,
    publish_attempted_pipeline_count: publishAttemptPipelineIds.size,
    publish_gate_passed_pipeline_count: publishGatePassedPipelineIds.size,
    publisher_committed_event_count: messageCommittedEvents.length,
    publisher_committed_pipeline_count: committedPipelineIds.size,
    publish_runtime_gate_blocked_count: publishGateEvents.filter((event) => {
      const details = typeof event.details_json === 'string'
        ? (() => { try { return JSON.parse(event.details_json); } catch (_error) { return {}; } })()
        : (event.details_json || {});
      return details.publish_gate_allowed === false || details.publish_gate_result === 'blocked';
    }).length,
    publish_retry_or_duplicate_event_count: publishRetryOrDuplicateEvents.length,
    published_pipeline_count: new Set(publishedPipelines.map(statPipelineId)).size,
    visible_agent_message_count: new Set(
      visibleAgentMessages.map((row) => String(row.message_id || row.id))
    ).size,
    expected_boundary_completed_count: completedBoundaryEvents.length,
    scenario_coverage_passed_count: statCoveragePassedCount(interventionCoverage),
    inhibition_case_passed_count: statCoveragePassedCount(inhibitionCoverage),
    optional_support_passed_count: statCoveragePassedCount(optionalSupportCoverage),
    optional_support_published_count: optionalSupportCoverage
      ? optionalSupportCoverage.supportPublishedCaseCount
      : 0,
    optional_support_not_published_count: optionalSupportCoverage
      ? optionalSupportCoverage.supportNotPublishedCaseCount
      : 0
  };
  return {
    coverageSource: 'pipeline_run_and_scoped_message_ids',
    counts,
    ...counts,
    expected_boundary_case_count: expectedInterventionCases.length,
    scenario_coverage_expected_count: statCoverageExpectedCount(
      interventionCoverage,
      expectedInterventionCases.length
    ),
    inhibition_case_expected_count: statCoverageExpectedCount(
      inhibitionCoverage,
      observationInhibitionCases.filter((item) => item.inhibitionStrategyId).length
    ),
    optional_support_expected_count: optionalSupportCases.length,
    message_complete: messageComplete,
    associationKeys: [
      'pipeline_run_id',
      'trigger_message_id',
      'published_message_id',
      'group_id',
      'discussion_id',
      'session_id',
      'scenario_id'
    ],
    associations: {
      pipeline_run_ids: [...new Set(pipelines.map(statPipelineId).filter(Boolean))],
      trigger_message_ids: completedBoundaryEvents
        .map((item) => item.trigger_message_id)
        .filter(hasValue)
        .map(String),
      published_message_ids: [...publishedMessageIds],
      visible_agent_message_ids: visibleAgentMessages
        .map((row) => row.message_id || row.id)
        .filter(hasValue)
        .map(String),
      scenario_ids: [...new Set(scenarios.map((item) => item.id).filter(Boolean))]
    },
    completed_boundaries: completedBoundaryEvents,
    unlinked_agent_message_count: messages.filter((row) => {
      const role = upper(row.role || row.sender_type);
      return role === 'AGENT' && !visibleAgentMessages.includes(row);
    }).length,
    input_integrity: messageInputIntegrity
  };
}

function formatCoverageStatisticsMarkdown(statistics) {
  const source = statistics || {};
  const counts = source.counts || source;
  const expected = counts.expected_intervention_cases || 0;
  return [
    `实际发布：${counts.published_pipeline_count || 0}`,
    `学生可见 Agent 消息：${counts.visible_agent_message_count || 0}`,
    `锁—消息—解锁完整边界：${counts.expected_boundary_completed_count || 0}`,
    `严格场景覆盖通过：${counts.scenario_coverage_passed_count || 0}/${expected}`,
    `抑制场景覆盖通过：${counts.inhibition_case_passed_count || 0}/${source.inhibition_case_expected_count || 0}`,
    `required_intervention state/route/published: ${counts.required_intervention_state_passed || 0}/` +
      `${counts.required_intervention_route_passed || 0}/${counts.required_intervention_published || 0}`,
    `optional_support state: ${counts.optional_support_state_passed || 0}/${counts.optional_support_case_count || 0}`,
    `observation_inhibition passed: ${counts.observation_inhibition_passed || 0}`,
    `cooldown/stale/superseded: ${counts.cooldown_suppressed || 0}/` +
      `${counts.stale_terminal || 0}/${counts.superseded_terminal || 0}`,
    `message_complete: ${counts.message_complete === true ? 'true' : 'false'}`
  ].join('\n');
}

function snapshotGroupsByCode(snapshot) {
  return new Map(((snapshot && snapshot.groups) || []).map((item) => [item.groupCode, item]));
}

function runIdentity(run) {
  return String(run && (run.pipeline_run_id || run.id || run.run_uuid || ''));
}

function batchIdentity(batch) {
  return String(batch && (batch.batch_id || batch.id || batch.window_key || ''));
}

function newAuditItems(baselineGroup, finalGroup, key, identity) {
  const baselineItems = (((baselineGroup && baselineGroup.audit) || {})[key] || []);
  const finalItems = (((finalGroup && finalGroup.audit) || {})[key] || []);
  const baselineIds = new Set(baselineItems.map(identity));
  return finalItems.filter((item) => !baselineIds.has(identity(item)));
}

function dbTable(group, tableName) {
  return ((((group || {}).dbAudit || {}).tables || {})[tableName]) || [];
}

function newDbItems(baselineGroup, finalGroup, tableName) {
  const baselineIds = new Set(dbTable(baselineGroup, tableName).map((item) => String(item.id)));
  return dbTable(finalGroup, tableName)
    .filter((item) => !baselineIds.has(String(item.id)));
}

function newDbItemsAcrossGroups(baselineSnapshot, finalSnapshot, tableName) {
  const baselineByGroup = snapshotGroupsByCode(baselineSnapshot);
  return ((finalSnapshot && finalSnapshot.groups) || []).flatMap((finalGroup) => (
    newDbItems(baselineByGroup.get(finalGroup.groupCode), finalGroup, tableName)
      .map((item) => ({ ...item, __groupCode: finalGroup.groupCode }))
  ));
}

function successfulScenarioEvents(events, scenarioId) {
  return (events || []).filter((event) => (
    event.type === 'message_success' &&
    event.scenarioId === scenarioId &&
    event.messageId !== undefined &&
    event.messageId !== null
  ));
}

function scriptedMessageKey(studentId, messageIndex, fallback) {
  return `${studentId || ''}:${messageIndex === undefined || messageIndex === null ? fallback : messageIndex}`;
}

function eventMessageIndex(event, fallback) {
  if (event.messageIndex !== undefined && event.messageIndex !== null) return event.messageIndex;
  if (event.message_index !== undefined && event.message_index !== null) return event.message_index;
  if (event.seq !== undefined && event.seq !== null) return event.seq;
  return fallback;
}

function buildScriptedMessageInputIntegrity(scenario, events = []) {
  const messages = ((scenario || {}).scriptedDiscussion || {}).messages || [];
  const expectedKeys = new Set(messages.map((message, index) => scriptedMessageKey(
    message.studentId || message.student_id,
    message.messageIndex === undefined
      ? (message.message_index === undefined ? message.seq : message.message_index)
      : message.messageIndex,
    index + 1
  )));
  const isScriptEvent = (event) => String(event.kind || '').startsWith('script:');
  const successEvents = (events || []).filter(
    (event) => event.type === 'message_success' && isScriptEvent(event)
  );
  const successKeys = successEvents.map((event, index) => scriptedMessageKey(
    event.studentId,
    eventMessageIndex(event, index + 1),
    index + 1
  ));
  const successfulKeys = new Set(successKeys.filter((key) => expectedKeys.has(key)));
  const failureEvents = (events || []).filter((event) => (
    event.type === 'message_failure' ||
    (event.type === 'message_delivery_final' &&
      (event.finalStatus || event.final_status || event.status) !== 'SUCCESS')
  ) && isScriptEvent(event));
  const failedKeys = new Set(failureEvents.map((event, index) => scriptedMessageKey(
    event.studentId,
    eventMessageIndex(event, index + 1),
    index + 1
  )));
  const missingKeys = [...expectedKeys].filter((key) => !successfulKeys.has(key));
  const duplicateSuccessKeys = [...new Set(successKeys.filter(
    (key, index) => successKeys.indexOf(key) !== index
  ))];
  const complete = expectedKeys.size === successfulKeys.size &&
    missingKeys.length === 0 && duplicateSuccessKeys.length === 0;
  return {
    status: complete ? 'COMPLETE' : 'INPUT_INCOMPLETE',
    coverageAllowed: complete,
    expected_script_messages: expectedKeys.size,
    successful_script_messages: successfulKeys.size,
    attempted_script_messages: new Set((events || [])
      .filter((event) => event.type === 'message_attempt' && isScriptEvent(event))
      .map((event, index) => scriptedMessageKey(
        event.studentId,
        eventMessageIndex(event, index + 1),
        index + 1
      ))).size,
    failed_script_messages: failedKeys.size,
    missing_message_keys: missingKeys,
    duplicate_success_keys: duplicateSuccessKeys,
    failure_statuses: [...new Set(failureEvents
      .map((event) => event.finalStatus || event.final_status || event.status)
      .filter(Boolean))],
    complete
  };
}

function scenarioMessageIds(events, scenarioId) {
  return unique(successfulScenarioEvents(events, scenarioId).map((event) => event.messageId));
}

function scenarioMessageSequences(events, scenarioId) {
  return unique(successfulScenarioEvents(events, scenarioId)
    .map((event) => event.seq)
    .filter((value) => value !== undefined && value !== null));
}

function precedingRecoveryMessageIds(events, scenarioId) {
  const ordered = (events || []).filter((event) => (
    event.type === 'message_success' && event.messageId !== undefined && event.messageId !== null
  ));
  const firstScenarioIndex = ordered.findIndex((event) => event.scenarioId === scenarioId);
  if (firstScenarioIndex <= 0) return [];
  const result = [];
  for (let index = firstScenarioIndex - 1; index >= 0; index -= 1) {
    const event = ordered[index];
    if (!['recovery', 'continuation'].includes(event.phase)) break;
    result.push(event.messageId);
  }
  return unique(result);
}

function precedingRecoveryMessageSequences(events, scenarioId) {
  const ordered = (events || []).filter((event) => (
    event.type === 'message_success' && event.messageId !== undefined && event.messageId !== null
  ));
  const firstScenarioIndex = ordered.findIndex((event) => event.scenarioId === scenarioId);
  if (firstScenarioIndex <= 0) return [];
  const result = [];
  for (let index = firstScenarioIndex - 1; index >= 0; index -= 1) {
    const event = ordered[index];
    if (!['recovery', 'continuation'].includes(event.phase)) break;
    if (event.seq !== undefined && event.seq !== null) result.push(event.seq);
  }
  return unique(result);
}

function allInSet(items, allowed) {
  return items.length > 0 && items.every((item) => allowed.has(String(item)));
}

function evidenceIds(item) {
  const raw = item && (
    item.evidence_message_ids ||
    item.evidenceMessageIds ||
    item.sub_state_evidence_message_ids
  );
  if (Array.isArray(raw)) return unique(raw);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? unique(parsed) : [];
  } catch (_error) {
    return unique(String(raw).split(',').map((value) => value.trim()));
  }
}

function evidenceSequences(item) {
  const raw = item && (
    item.evidence_sequences ||
    item.evidenceSequences ||
    item.suppression_evidence_sequences
  );
  if (Array.isArray(raw)) return unique(raw);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? unique(parsed) : [];
  } catch (_error) {
    return unique(String(raw).split(',').map((value) => value.trim()));
  }
}

function evidenceDomains(
  events,
  scenarioId,
  { includeRecovery = false, messageRows = [] } = {}
) {
  const messageIds = scenarioMessageIds(events, scenarioId);
  const scriptedSequences = scenarioMessageSequences(events, scenarioId);
  if (includeRecovery) {
    messageIds.push(...precedingRecoveryMessageIds(events, scenarioId));
    scriptedSequences.push(...precedingRecoveryMessageSequences(events, scenarioId));
  }
  const idSet = new Set(unique(messageIds));
  const mappedSequences = unique((messageRows || [])
    .filter((row) => idSet.has(String(row.message_id || row.id)))
    .map((row) => row.sequence)
    .filter((value) => value !== undefined && value !== null));
  return {
    messageIds: idSet,
    // Prefer authoritative discussion sequences. Script sequence is only a
    // fallback for older audits that do not expose the scoped messages table.
    messageSequences: new Set(mappedSequences.length ? mappedSequences : unique(scriptedSequences))
  };
}

function recordEvidenceCoordinates(item, domains) {
  const sequences = evidenceSequences(item);
  if (sequences.some((value) => domains.messageSequences.has(String(value)))) {
    return { values: sequences, allowed: domains.messageSequences, domain: 'sequence' };
  }
  const ids = evidenceIds(item);
  if (ids.some((value) => domains.messageIds.has(String(value)))) {
    return { values: ids, allowed: domains.messageIds, domain: 'message_id' };
  }
  // The three-stage pipeline historically stores discussion sequences in its
  // evidence_message_ids field. Treat them as sequences only when they match
  // the scripted sequence domain; unrelated numeric IDs remain rejected.
  if (ids.some((value) => domains.messageSequences.has(String(value)))) {
    return { values: ids, allowed: domains.messageSequences, domain: 'sequence_compat' };
  }
  return { values: sequences.length ? sequences : ids, allowed: new Set(), domain: null };
}

function evidenceSomeInDomains(item, domains) {
  const coordinates = recordEvidenceCoordinates(item, domains);
  return coordinates.values.some((value) => coordinates.allowed.has(String(value)));
}

function evidenceAllInDomains(item, domains) {
  const coordinates = recordEvidenceCoordinates(item, domains);
  return allInSet(coordinates.values, coordinates.allowed);
}

function scopeMatches(item, scope) {
  if (!item || !scope) return false;
  return Number(item.group_id) === Number(scope.group_id) &&
    Number(item.session_id) === Number(scope.session_id) &&
    Number(item.discussion_id) === Number(scope.discussion_id);
}

function listValue(value) {
  if (Array.isArray(value)) return value;
  if (!value) return [];
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  } catch (_error) {
    return String(value).split(',').map((item) => item.trim()).filter(Boolean);
  }
}

function strategyRouteIds(item) {
  return unique([
    item && item.selected_strategy_id,
    item && item.inhibition_strategy_id,
    ...listValue(item && (item.supporting_strategy_ids || item.supporting_strategy_ids_json)),
    ...listValue(item && (item.strategy_candidate_ids || item.candidate_strategy_ids))
  ]);
}

function strategyBehaviorMatches(stateScenario, item) {
  const classification = classifyScenario(stateScenario);
  const route = routeForScenario(stateScenario);
  const allowed = new Set(route.runtimeAllowedStrategyIds);
  const published = [item && item.publish_status, item && item.final_status]
    .some((value) => String(value || '').toUpperCase() === 'PUBLISHED');
  const shouldIntervene = ['1', 'true', 'yes', '是'].includes(
    String(item && item.should_intervene || '').toLowerCase()
  );
  if (stateScenario.skipStrategyCoverage) {
    return item && !published && !shouldIntervene &&
      !item.selected_strategy_id && !item.inhibition_strategy_id;
  }
  if (stateScenario.inhibitionStrategyId) {
    return item && item.inhibition_strategy_id === stateScenario.inhibitionStrategyId &&
      isStrategyAllowedForScenario(stateScenario, stateScenario.inhibitionStrategyId) &&
      !published && !shouldIntervene;
  }
  if (classification.type === SCENARIO_TYPES.REQUIRED_INTERVENTION) {
    return item && allowed.has(String(item.selected_strategy_id || '')) &&
      shouldIntervene;
  }
  if (classification.type === SCENARIO_TYPES.OPTIONAL_SUPPORT) {
    if (published) {
      return item && allowed.has(String(item.selected_strategy_id || '')) && shouldIntervene;
    }
    const routeIds = strategyRouteIds(item);
    return item && (routeIds.length === 0 || routeIds.some((strategyId) => allowed.has(strategyId)));
  }
  return item && !published &&
    strategyRouteIds(item).some((strategyId) => allowed.has(strategyId));
}

function buildPlannedScriptCoverage(scenario) {
  const scenarios = scriptedScenarios(scenario);
  return {
    coverageSource: 'scenario_plan_only',
    validationMode: 'real_coverage',
    plannedCaseCount: scenarios.length,
    plannedStates: unique(scenarios.map((item) => (
      item.expectedFailureFallback ? 'unclassified' : item.canonicalSubState
    ))),
    scenarios: scenarios.map((item) => ({
      scenarioId: item.id,
      groupCode: item.groupCode,
      plannedState: item.expectedFailureFallback ? 'unclassified' : item.canonicalSubState,
      plannedMessageCount: (item.messages || []).length
    })),
    passed: scenarios.length > 0 && scenarios.every((item) => (item.messages || []).length > 0)
  };
}

function buildMessageSendCoverage(scenario, events, inputIntegrity = null) {
  const messages = ((scenario || {}).scriptedDiscussion || {}).messages || [];
  const scenarios = scriptedScenarios(scenario);
  const results = scenarios.map((item) => {
    const expected = messages.filter((message) => message.scenarioId === item.id);
    const sent = successfulScenarioEvents(events, item.id);
    const messageIds = unique(sent.map((event) => event.messageId));
    const expectedIndexes = new Set(expected.map((message, index) => String(
      message.messageIndex ?? message.message_index ?? message.seq ?? index + 1
    )));
    const sentIndexes = sent.map((event, index) => String(eventMessageIndex(event, index + 1)));
    const sentIndexSet = new Set(sentIndexes);
    const missingIndexes = [...expectedIndexes].filter((index) => !sentIndexSet.has(index));
    const finalEvents = (events || []).filter((event) => (
      (event.type === 'message_delivery_final' || event.type === 'message_failure') &&
      event.scenarioId === item.id &&
      (event.finalStatus || event.final_status || event.status) !== 'SUCCESS'
    ));
    const failedStatuses = unique(finalEvents
      .map((event) => event.final_status || event.finalStatus || event.status)
      .filter((status) => status && status !== 'SUCCESS'));
    const expectedMessageCount = expected.length || (item.messages || []).length;
    return {
      scenarioId: item.id,
      expectedMessageCount,
      sentMessageCount: sent.length,
      failedMessageCount: finalEvents.length,
      missingMessageIndexes: missingIndexes,
      failureStatuses: failedStatuses,
      persistedMessageIds: messageIds,
      passed: sent.length === expectedMessageCount &&
        missingIndexes.length === 0 &&
        messageIds.length === sent.length &&
        failedStatuses.length === 0
    };
  });
  const computedInputIntegrity = buildScriptedMessageInputIntegrity(scenario, events);
  return {
    coverageSource: 'message_success_events_with_server_message_ids',
    inputIntegrity: inputIntegrity || computedInputIntegrity,
    status: (inputIntegrity || computedInputIntegrity).status,
    scenarios: results,
    passed: (inputIntegrity ? inputIntegrity.complete : true) &&
      results.length > 0 && results.every((item) => item.passed)
  };
}

function finalSubState(run) {
  const value = run && (run.final_sub_state_code || run.canonical_sub_state_code);
  return value && value !== '历史数据未记录' ? String(value) : null;
}

function overlayTags(run) {
  return unique(listValue(
    run && (
      run.secondary_sub_state_tags ||
      run.secondary_sub_state_tags_json ||
      run.secondary_tags ||
      run.state_overlays
    )
  ));
}

function hasExpectedOverlayTags(stateScenario, record) {
  const expected = expectedOverlayStates(stateScenario);
  if (!expected.length) return true;
  const observed = new Set(overlayTags(record));
  return expected.every((tag) => observed.has(tag));
}

function configuredAcceptance(scenario) {
  return (
    scenario &&
    scenario.strategyAudit &&
    scenario.strategyAudit.interventionAcceptance
  ) || {};
}

function positiveInteger(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : fallback;
}

function buildActualStateCoverage(scenario, baselineSnapshot, finalSnapshot, events = []) {
  const scenarios = scriptedScenarios(scenario).filter((item) => !item.expectedFailureFallback);
  if (!scenarios.length) return null;

  const baselineByGroup = snapshotGroupsByCode(baselineSnapshot);
  const finalByGroup = snapshotGroupsByCode(finalSnapshot);
  const results = scenarios.map((item) => {
    const classification = classifyScenario(item);
    const newRuns = newAuditItems(
      baselineByGroup.get(item.groupCode),
      finalByGroup.get(item.groupCode),
      'strategy_pipeline_runs',
      runIdentity
    );
    const expectedTags = classification.overlayStates;
    const scopedRuns = scenarioScopedPipelineRuns(newRuns, events, item.id);
    const stateRuns = scopedRuns.runs.filter((run) => finalSubState(run) === classification.primaryState);
    const matchingRuns = stateRuns.filter((run) => {
      const observedTags = new Set(overlayTags(run));
      return expectedTags.every((tag) => observedTags.has(tag));
    });
    return {
      scenarioId: item.id,
      groupCode: item.groupCode,
      scenarioType: classification.type,
      expectedSubState: classification.primaryState,
      expectedPrimaryState: classification.primaryState,
      expectedOverlayTags: expectedTags,
      expectedOverlayStates: expectedTags,
      observedSubStates: unique(newRuns.map(finalSubState)),
      observedOverlayTags: unique(stateRuns.flatMap(overlayTags)),
      triggerMessageId: scopedRuns.triggerMessageId,
      exactTriggerMatch: Boolean(scopedRuns.exactMatch),
      matchingRunCount: matchingRuns.length,
      primaryStatePassed: stateRuns.length > 0,
      overlayPassed: matchingRuns.length > 0,
      passed: matchingRuns.length > 0
    };
  });

  const expectedStates = unique(scenarios.map(expectedPrimaryState));
  const observedStates = unique(results.flatMap((item) => item.observedSubStates));
  const overlayResults = results.filter((item) => item.expectedOverlayStates.length > 0);
  const canonicalStateCoverage = {
    expectedStateCount: expectedStates.length,
    observedStateCount: unique(results.filter((item) => item.primaryStatePassed)
      .map((item) => item.expectedPrimaryState)).length,
    expectedStates,
    observedStates,
    missingStates: expectedStates.filter((state) => !observedStates.includes(state)),
    passed: results.every((item) => item.primaryStatePassed)
  };
  const overlayCoverage = {
    expectedCaseCount: overlayResults.length,
    passedCaseCount: overlayResults.filter((item) => item.overlayPassed).length,
    scenarios: overlayResults,
    passed: overlayResults.every((item) => item.overlayPassed)
  };
  return {
    coverageSource: 'teacher_agent_audit_strategy_pipeline_runs',
    auditAvailable: Boolean(finalSnapshot && finalSnapshot.sessionId),
    expectedStates,
    observedStates,
    missingStates: expectedStates.filter((state) => !observedStates.includes(state)),
    canonicalStateCoverage,
    overlayCoverage,
    scenarios: results,
    passed: Boolean(finalSnapshot && finalSnapshot.sessionId) &&
      canonicalStateCoverage.passed && overlayCoverage.passed
  };
}

function buildModelFailureCoverage(scenario, baselineSnapshot, finalSnapshot) {
  const scenarios = scriptedScenarios(scenario).filter((item) => item.expectedFailureFallback);
  if (!scenarios.length) return null;

  const baselineByGroup = snapshotGroupsByCode(baselineSnapshot);
  const finalByGroup = snapshotGroupsByCode(finalSnapshot);
  const results = scenarios.map((item) => {
    const baselineGroup = baselineByGroup.get(item.groupCode);
    const finalGroup = finalByGroup.get(item.groupCode);
    const newBatches = newAuditItems(
      baselineGroup,
      finalGroup,
      'assessment_batches',
      batchIdentity
    );
    const failedBatches = newBatches.filter((batch) => {
      const terminal = String(batch.terminal_status || batch.status || '').toLowerCase();
      return Boolean(batch.error_code) &&
        ['failed', 'degraded', 'quarantined', 'superseded'].includes(terminal) &&
        Number(batch.fallback_segment_count || 0) > 0 &&
        batch.assignment_source === 'batch_unclassified';
    });
    const newRuns = newAuditItems(
      baselineGroup,
      finalGroup,
      'strategy_pipeline_runs',
      runIdentity
    );
    const publishedRuns = newRuns.filter(
      (run) => String(run.publish_status || run.final_status || '').toUpperCase() === 'PUBLISHED'
    );
    return {
      scenarioId: item.id,
      groupCode: item.groupCode,
      newBatchCount: newBatches.length,
      failedBatchCount: failedBatches.length,
      errorCodes: unique(failedBatches.map((batch) => batch.error_code)),
      fallbackSegmentCount: failedBatches.reduce(
        (sum, batch) => sum + Number(batch.fallback_segment_count || 0),
        0
      ),
      publishedStrategyRunCount: publishedRuns.length,
      passed: failedBatches.length > 0 && publishedRuns.length === 0
    };
  });
  return {
    coverageSource: 'teacher_agent_audit_assessment_batches',
    scenarios: results,
    passed: Boolean(finalSnapshot && finalSnapshot.sessionId) && results.every((item) => item.passed)
  };
}

function buildAgentLockCoverage(scenario, baselineSnapshot, finalSnapshot) {
  if (!(scenario && scenario.stateSuite && scenario.stateSuite.requireAgentLockRecovery)) return null;

  const baselineByGroup = snapshotGroupsByCode(baselineSnapshot);
  const finalByGroup = snapshotGroupsByCode(finalSnapshot);
  const groupCodes = unique(scriptedScenarios(scenario).map((item) => item.groupCode));
  const groups = groupCodes.map((groupCode) => {
    const baselineGroup = baselineByGroup.get(groupCode);
    const finalGroup = finalByGroup.get(groupCode);
    const dbRuns = newDbItems(
      baselineGroup,
      finalGroup,
      'strategy_pipeline_runs'
    );
    const newRuns = dbRuns.length ? dbRuns : newAuditItems(
      baselineGroup,
      finalGroup,
      'strategy_pipeline_runs',
      runIdentity
    );
    const latencyRows = newDbItems(baselineGroup, finalGroup, 'strategy_pipeline_latency_events');
    const acquiredIds = new Set(
      latencyRows
        .filter((event) => event.event === 'lock_acquired')
        .map((event) => String(event.pipeline_run_id || ''))
        .filter(Boolean)
    );
    const hasAcquiredLease = (run) => Boolean(
      run.room_lock_acquired_at ||
      run.room_lock_token ||
      acquiredIds.has(runIdentity(run))
    );
    const lockFreePreliminaryRuns = newRuns.filter((run) => (
      !hasAcquiredLease(run) &&
      !TERMINAL_PIPELINE_STATUSES.has(String(run.final_status || '').toUpperCase()) &&
      !run.stage2_started_at &&
      ['PENDING', ''].includes(String(run.stage2_status || '').toUpperCase()) &&
      ['NOT_READY', ''].includes(String(run.publish_status || '').toUpperCase())
    ));
    const ignoredIds = new Set(lockFreePreliminaryRuns.map(runIdentity));
    const activeRuns = newRuns.filter((run) => (
      hasAcquiredLease(run) &&
      !TERMINAL_PIPELINE_STATUSES.has(String(run.final_status || '').toUpperCase())
    ));
    const acquiredRunIds = newRuns.filter(hasAcquiredLease).map(runIdentity);
    const publishedRuns = newRuns.filter(
      (run) => String(run.publish_status || run.final_status || '').toUpperCase() === 'PUBLISHED'
    );
    return {
      groupCode,
      newPipelineRunCount: newRuns.length,
      acquiredRunIds,
      publishedRunCount: publishedRuns.length,
      ignoredLockFreePreliminaryRunIds: [...ignoredIds],
      activeRunIds: activeRuns.map(runIdentity),
      passed: activeRuns.length === 0
    };
  });
  const emotionAgentEnabled = Boolean(
    finalSnapshot && finalSnapshot.agentFlags && finalSnapshot.agentFlags.emotionAgentEnabled
  );
  return {
    coverageSource: 'teacher_status_and_agent_audit',
    emotionAgentEnabled,
    groups,
    // The six-group suite deliberately runs with the emotion Agent disabled.
    // Lock recovery is about terminal pipeline/lease state; it must not fail
    // merely because the optional emotion Agent is isolated off.
    passed: groups.every((item) => item.passed)
  };
}

function buildCanonicalDbCoverage(scenario, baselineSnapshot, finalSnapshot, events) {
  const scenarios = scriptedScenarios(scenario);
  const baselineByGroup = snapshotGroupsByCode(baselineSnapshot);
  const finalByGroup = snapshotGroupsByCode(finalSnapshot);
  const results = scenarios.map((item) => {
    const baselineGroup = baselineByGroup.get(item.groupCode);
    const finalGroup = finalByGroup.get(item.groupCode);
    const scope = finalGroup && finalGroup.dbAudit && finalGroup.dbAudit.scope;
    const messageRows = dbTable(finalGroup, 'messages');
    const sentDomains = evidenceDomains(events, item.id, { messageRows });
    const allowedDomains = evidenceDomains(events, item.id, {
      includeRecovery: true,
      messageRows
    });
    const sentIds = sentDomains.messageIds;
    const newMessages = newDbItems(baselineGroup, finalGroup, 'messages');
    const newSegments = newDbItems(
      baselineGroup,
      finalGroup,
      'collaboration_state_segments'
    );
    const newBatches = newDbItems(
      baselineGroup,
      finalGroup,
      'state_assessment_batches'
    );
    const newRuns = newDbItems(
      baselineGroup,
      finalGroup,
      'strategy_pipeline_runs'
    );
    const scopedRuns = scenarioScopedPipelineRuns(newRuns, events, item.id);
    const newInterventions = newDbItems(
      baselineGroup,
      finalGroup,
      'intervention_runs'
    );
    const expectedTags = unique(item.expectedOverlayTags);
    const hasExpectedTags = (record) => {
      const observed = new Set(overlayTags(record));
      return expectedTags.every((tag) => observed.has(tag));
    };
    const matchingSegments = newSegments.filter(
      (segment) => segment.canonical_sub_state_code === item.canonicalSubState &&
        hasExpectedTags(segment)
    );
    const matchingRuns = scopedRuns.runs.filter(
      (run) => run.canonical_sub_state_code === item.canonicalSubState &&
        hasExpectedTags(run)
    );
    const matchingRecords = [...matchingSegments, ...matchingRuns];
    const scenarioRecords = matchingRecords.filter((record) => (
      evidenceSomeInDomains(record, sentDomains)
    ));
    const evidenceMatches = scenarioRecords.filter((record) => (
      (scopedRuns.triggerMessageId &&
        String(record.trigger_message_id || '') === scopedRuns.triggerMessageId) ||
      evidenceAllInDomains(record, allowedDomains)
    ));
    const duplicateKeys = new Map();
    for (const segment of newSegments) {
      const key = [
        segment.canonical_sub_state_code ||
          segment.assessment_status ||
          segment.state_code ||
          '',
        segment.start_sequence ?? segment.start_message_id ?? '',
        segment.end_sequence ?? segment.end_message_id ?? '',
        segment.source || ''
      ].join('|');
      duplicateKeys.set(key, (duplicateKeys.get(key) || 0) + 1);
    }
    const duplicates = [...duplicateKeys.entries()]
      .filter(([, count]) => count > 1)
      .map(([key, count]) => ({ key, count }));
    const scopedItems = [
      ...newMessages,
      ...newSegments,
      ...newBatches,
      ...newRuns,
      ...newInterventions
    ];
    const scopePassed = Boolean(scope) && scopedItems.every((record) => scopeMatches(record, scope));

    if (item.expectedFailureFallback) {
      const failedBatches = newBatches.filter((batch) => (
        Boolean(batch.error_code) &&
        Number(batch.fallback_segment_count || 0) > 0 &&
        ['failed', 'degraded', 'quarantined', 'superseded'].includes(
          String(batch.terminal_status || batch.status || '').toLowerCase()
        )
      ));
      const fallbackSegments = newSegments.filter((segment) => (
        !segment.canonical_sub_state_code &&
        segment.assessment_status === 'unclassified' &&
        (
          evidenceAllInDomains(segment, sentDomains) ||
          sentIds.has(String(segment.start_message_id)) ||
          sentIds.has(String(segment.end_message_id)) ||
          sentDomains.messageSequences.has(String(segment.start_sequence)) ||
          sentDomains.messageSequences.has(String(segment.end_sequence))
        )
      ));
      return {
        scenarioId: item.id,
        groupCode: item.groupCode,
        expectedStatus: 'unclassified',
        failedBatchCount: failedBatches.length,
        fallbackSegmentCount: fallbackSegments.length,
        scopePassed,
        duplicateSegments: duplicates,
        passed: sentIds.size > 0 && failedBatches.length > 0 &&
          fallbackSegments.length > 0 && scopePassed && duplicates.length === 0
      };
    }

    const behaviorMatches = matchingRuns.filter((run) => strategyBehaviorMatches(item, run));
    const crossCaseRecords = scenarioRecords.filter((record) => {
      if (scopedRuns.triggerMessageId &&
          String(record.trigger_message_id || '') === scopedRuns.triggerMessageId) {
        return false;
      }
      const coordinates = recordEvidenceCoordinates(record, allowedDomains);
      return coordinates.values.length > 0 &&
        !allInSet(coordinates.values, coordinates.allowed);
    });
    return {
      scenarioId: item.id,
      groupCode: item.groupCode,
      expectedSubState: item.canonicalSubState,
      expectedOverlayTags: expectedTags,
      sentMessageIds: [...sentIds],
      sentMessageSequences: [...sentDomains.messageSequences],
      persistedSegmentIds: matchingSegments.map((segment) => segment.id),
      persistedPipelineRunIds: matchingRuns.map((run) => run.id),
      triggerMessageId: scopedRuns.triggerMessageId,
      exactTriggerMatch: Boolean(scopedRuns.exactMatch),
      evidenceMatchedRecordCount: evidenceMatches.length,
      strategyBehaviorMatchedRunCount: behaviorMatches.length,
      crossCaseRecordIds: crossCaseRecords.map((record) => record.id),
      scopePassed,
      duplicateSegments: duplicates,
      passed: sentIds.size > 0 &&
        (matchingSegments.length > 0 || matchingRuns.length > 0) &&
        evidenceMatches.length > 0 &&
        behaviorMatches.length > 0 &&
        crossCaseRecords.length === 0 &&
        scopePassed &&
        duplicates.length === 0
    };
  });
  const available = Boolean(finalSnapshot && finalSnapshot.auditAvailable) &&
    [...finalByGroup.values()].every((group) => (
      group.dbAudit &&
      group.dbAudit.audit_available &&
      ['messages', 'collaboration_state_segments', 'state_assessment_batches',
        'strategy_pipeline_runs', 'intervention_runs',
        'emotion_reflection_slots', 'strategy_pipeline_latency_events'].every(
        (tableName) => group.dbAudit.available_tables &&
          group.dbAudit.available_tables[tableName]
      )
    ));
  return {
    coverageSource: 'privacy_minimised_state_suite_db_audit',
    auditAvailable: available,
    scenarios: results,
    passed: available && results.length > 0 && results.every((item) => item.passed)
  };
}

function apiStateValues(payload) {
  return unique([
    payload && payload.current_state && payload.current_state.final_sub_state_code,
    ...(((payload && payload.state_segments) || []).map(
      (item) => item.final_sub_state_code || item.canonical_sub_state_code
    )),
    ...(((payload && payload.canonical_segments) || []).map(
      (item) => item.final_sub_state_code || item.canonical_sub_state_code
    )),
    ...(((payload && payload.messages) || []).map(
      (item) => item.final_sub_state_code
    ))
  ]);
}

function buildTeacherApiCoverage(scenario, finalSnapshot, events) {
  const scenarios = scriptedScenarios(scenario);
  const finalByGroup = snapshotGroupsByCode(finalSnapshot);
  const results = scenarios.map((item) => {
    const group = finalByGroup.get(item.groupCode);
    const apis = (group && group.teacherApis) || {};
    const scope = group && group.dbAudit && group.dbAudit.scope;
    const sentDomains = evidenceDomains(events, item.id, {
      messageRows: dbTable(group, 'messages')
    });
    const allowedDomains = evidenceDomains(events, item.id, {
      includeRecovery: true,
      messageRows: dbTable(group, 'messages')
    });
    const sentIds = sentDomains.messageIds;
    const scopedSegments = (payload, key) => (
      ((payload && payload[key]) || []).filter((segment) => (
        !scope || Number(segment.discussion_id) === Number(scope.discussion_id)
      ))
    );
    const trendStates = unique(scopedSegments(apis.emotionTrend, 'state_segments').map(
      (segment) => segment.final_sub_state_code || segment.canonical_sub_state_code
    ));
    const reviewStates = unique([
      ...scopedSegments(apis.emotionReview, 'state_segments').map(
        (segment) => segment.final_sub_state_code || segment.canonical_sub_state_code
      ),
      ...(((apis.emotionReview || {}).messages) || [])
        .filter((message) => sentIds.has(String(message.id)))
        .map((message) => message.final_sub_state_code)
    ]);
    const allAuditRuns = (((apis.agentAudit || {}).strategy_pipeline_runs) || [])
      .filter((run) => (
        !scope || Number(run.discussion_id) === Number(scope.discussion_id)
      ));
    const scopedAuditRuns = scenarioScopedPipelineRuns(allAuditRuns, events, item.id);
    const auditRuns = scopedAuditRuns.runs.filter((run) => (
      (scopedAuditRuns.exactMatch &&
        String(run.trigger_message_id || '') === String(scopedAuditRuns.triggerMessageId)) ||
      evidenceAllInDomains(run, allowedDomains)
    ));
    const auditStates = unique(auditRuns.map(finalSubState));
    const expectedTags = unique(item.expectedOverlayTags);
    const auditOverlayTags = unique(auditRuns.flatMap(overlayTags));
    const detailStates = unique(scopedSegments(apis.groupDetail, 'canonical_segments').map(
      (segment) => segment.final_sub_state_code || segment.canonical_sub_state_code
    ));
    const reviewMessages = ((apis.emotionReview || {}).messages || [])
      .filter((message) => sentIds.has(String(message.id)));
    const assignmentComplete = reviewMessages.length === sentIds.size &&
      reviewMessages.every((message) => (
        ['confirmed', 'observing', 'unclassified'].includes(message.assessment_status) &&
        Boolean(message.assignment_source)
      ));
    if (item.expectedFailureFallback) {
      const failedBatches = (((apis.agentAudit || {}).assessment_batches) || [])
        .filter((batch) => (
          Boolean(batch.error_code) &&
          Number(batch.fallback_segment_count || 0) > 0
        ));
      const unclassifiedVisible = reviewMessages.length > 0 &&
        reviewMessages.every((message) => (
          !message.final_sub_state_code &&
          message.assessment_status === 'unclassified'
        ));
      return {
        scenarioId: item.id,
        expectedStatus: 'unclassified',
        assignmentComplete,
        failedBatchCount: failedBatches.length,
        unclassifiedVisible,
        passed: assignmentComplete && failedBatches.length > 0 && unclassifiedVisible
      };
    }
    if (item.expectedProcessState === 'unknown_sub_state') {
      const processVisible = reviewMessages.length > 0 && reviewMessages.every((message) => (
        !message.final_sub_state_code &&
        message.assessment_status === 'unclassified' &&
        Boolean(message.assignment_source) &&
        !message.error_code
      ));
      return {
        scenarioId: item.id,
        expectedProcessState: item.expectedProcessState,
        assignmentComplete,
        agentAuditVisible: auditStates.includes(item.expectedProcessState),
        processVisible,
        passed: assignmentComplete &&
          auditStates.includes(item.expectedProcessState) && processVisible
      };
    }
    return {
      scenarioId: item.id,
      expectedSubState: item.canonicalSubState,
      expectedOverlayTags: expectedTags,
      triggerMessageId: scopedAuditRuns.triggerMessageId,
      exactTriggerMatch: Boolean(scopedAuditRuns.exactMatch),
      emotionTrendVisible: trendStates.includes(item.canonicalSubState),
      emotionReviewVisible: reviewStates.includes(item.canonicalSubState),
      agentAuditVisible: auditStates.includes(item.canonicalSubState),
      groupDetailVisible: detailStates.includes(item.canonicalSubState),
      overlayVisible: expectedTags.every((tag) => auditOverlayTags.includes(tag)),
      assignmentComplete,
      passed: trendStates.includes(item.canonicalSubState) &&
        reviewStates.includes(item.canonicalSubState) &&
        auditStates.includes(item.canonicalSubState) &&
        detailStates.includes(item.canonicalSubState) &&
        expectedTags.every((tag) => auditOverlayTags.includes(tag)) &&
        assignmentComplete
    };
  });
  const finalScenario = scenarios[scenarios.length - 1];
  const finalGroup = finalScenario && finalByGroup.get(finalScenario.groupCode);
  const groupRows = ((((finalGroup || {}).teacherApis || {}).teacherGroups || {}).groups || []);
  const currentGroup = groupRows.find(
    (item) => Number(item.group_id) === Number(finalGroup && finalGroup.groupId)
  );
  const observedCurrentState = currentGroup && (
    currentGroup.final_sub_state_code ||
    (
      ['observing', 'unclassified'].includes(currentGroup.assessment_status)
        ? currentGroup.assessment_status
        : null
    )
  );
  const expectedCurrentStates = finalScenario && !finalScenario.expectedFailureFallback
    ? new Set(
      [
        finalScenario.expectedProcessState === 'unknown_sub_state'
          ? 'unclassified'
          : finalScenario.canonicalSubState,
        ...(finalScenario.detectedSelfRegulation
          ? ['standard', 'execution_progress']
          : []),
        ...(finalScenario.shouldIntervene ? ['observing'] : [])
      ]
    )
    : null;
  const groupListPassed = Boolean(currentGroup) && (
    expectedCurrentStates
      ? expectedCurrentStates.has(observedCurrentState)
      : currentGroup.assessment_status === 'unclassified'
  );
  const reviewAgentMessages = [...finalByGroup.values()].flatMap((group) => (
    ((((group || {}).teacherApis || {}).emotionReview || {}).messages || [])
      .filter((message) => String(message.role || message.sender_type || '').toLowerCase() === 'agent')
  ));
  const agentMessagesClean = reviewAgentMessages.every(
    (message) => !message.final_sub_state_code
  );
  const finalValues = [...finalByGroup.values()].flatMap((group) => {
    const apis = group.teacherApis || {};
    return [
      ...apiStateValues(apis.emotionTrend),
      ...apiStateValues(apis.emotionReview),
      ...apiStateValues(apis.groupDetail),
      ...unique(((apis.agentAudit || {}).strategy_pipeline_runs || []).map(finalSubState)),
      ...(((apis.teacherGroups || {}).groups || []).map((item) => item.final_sub_state_code))
    ];
  });
  const legacyFinalValues = unique(finalValues.filter((value) => LEGACY_STATE_CODES.has(value)));
  const available = Boolean(finalSnapshot && finalSnapshot.auditAvailable) &&
    [...finalByGroup.values()].every((group) => (
      group.teacherApis &&
      group.teacherApis.emotionTrend &&
      group.teacherApis.emotionReview &&
      group.teacherApis.agentAudit &&
      group.teacherApis.teacherGroups &&
      group.teacherApis.groupDetail
    ));
  return {
    coverageSource: 'teacher_emotion_trend_review_agent_audit_groups_detail',
    auditAvailable: available,
    groupListPassed,
    expectedCurrentStates: expectedCurrentStates
      ? [...expectedCurrentStates]
      : [],
    observedCurrentState,
    agentMessagesClean,
    legacyFinalValues,
    scenarios: results,
    passed: available && groupListPassed && agentMessagesClean &&
      legacyFinalValues.length === 0 &&
      results.length > 0 && results.every((item) => item.passed)
  };
}

function exportRows(group, filename) {
  return (((group || {}).exportAudit || {}).files || {})[filename] || [];
}

function buildExportCoverage(scenario, finalSnapshot, events) {
  const scenarios = scriptedScenarios(scenario);
  const finalByGroup = snapshotGroupsByCode(finalSnapshot);
  const results = scenarios.map((item) => {
    const group = finalByGroup.get(item.groupCode);
    const sentDomains = evidenceDomains(events, item.id, {
      messageRows: dbTable(group, 'messages')
    });
    const allowedDomains = evidenceDomains(events, item.id, {
      includeRecovery: true,
      messageRows: dbTable(group, 'messages')
    });
    const sentIds = sentDomains.messageIds;
    const scopedDbRuns = scenarioScopedPipelineRuns(
      dbTable(group, 'strategy_pipeline_runs'),
      events,
      item.id
    );
    const matchingDbRuns = scopedDbRuns.runs.filter((run) => (
      run.canonical_sub_state_code === item.canonicalSubState &&
      ((scopedDbRuns.exactMatch &&
        String(run.trigger_message_id || '') === String(scopedDbRuns.triggerMessageId)) ||
        evidenceAllInDomains(run, allowedDomains))
    ));
    const matchingDbRunIds = new Set(
      matchingDbRuns.map((run) => String(run.id || run.pipeline_run_id))
    );
    const messageRows = exportRows(group, 'messages.csv')
      .filter((row) => sentIds.has(String(row.message_id || row.id)));
    const strategyRows = exportRows(group, 'strategy_pipeline_runs.csv')
      .filter((row) => (
        row.canonical_sub_state_code === item.canonicalSubState &&
        matchingDbRunIds.has(String(row.pipeline_run_id || row.id))
      ));
    const expectedTags = unique(item.expectedOverlayTags);
    const interventionRows = exportRows(group, 'interventions.csv')
      .filter((row) => (
        row.canonical_sub_state_code === item.canonicalSubState &&
        matchingDbRunIds.has(String(row.strategy_pipeline_run_id))
      ));
    const unifiedRows = exportRows(group, 'unified-events.csv')
      .filter((row) => (
        row.event_type === 'state_assignment' &&
        sentIds.has(String(row.related_id || row.event_id))
      ));
    const assignmentComplete = messageRows.length === sentIds.size &&
      messageRows.every((row) => (
        ['confirmed', 'observing', 'unclassified'].includes(row.assessment_status) &&
        Boolean(row.assignment_source)
      ));
    if (item.expectedFailureFallback) {
      const unclassified = messageRows.length > 0 && messageRows.every((row) => (
        !row.final_sub_state_code &&
        row.assessment_status === 'unclassified' &&
        Boolean(row.error_code)
      ));
      const unifiedUnclassified = unifiedRows.length > 0 && unifiedRows.every((row) => (
        !row.final_sub_state_code &&
        row.assessment_status === 'unclassified'
      ));
      return {
        scenarioId: item.id,
        expectedStatus: 'unclassified',
        assignmentComplete,
        unclassified,
        unifiedUnclassified,
        passed: assignmentComplete && unclassified && unifiedUnclassified
      };
    }
    if (item.expectedProcessState === 'unknown_sub_state') {
      const processMessages = messageRows.length > 0 && messageRows.every((row) => (
        !row.final_sub_state_code &&
        row.assessment_status === 'unclassified' &&
        Boolean(row.assignment_source) &&
        !row.error_code
      ));
      const unifiedProcess = unifiedRows.length > 0 && unifiedRows.every((row) => (
        !row.final_sub_state_code && row.assessment_status === 'unclassified'
      ));
      const strategyVisible = strategyRows.some(
        (row) => strategyBehaviorMatches(item, row)
      );
      return {
        scenarioId: item.id,
        expectedProcessState: item.expectedProcessState,
        assignmentComplete,
        processMessages,
        strategyVisible,
        unifiedProcess,
        passed: assignmentComplete && processMessages && strategyVisible && unifiedProcess
      };
    }
    const messageVisible = messageRows.some(
      (row) => row.final_sub_state_code === item.canonicalSubState
    );
    const strategyVisible = strategyRows.some(
      (row) => strategyBehaviorMatches(item, row) &&
        expectedTags.every((tag) => overlayTags(row).includes(tag))
    );
    const unifiedVisible = unifiedRows.some(
      (row) => row.final_sub_state_code === item.canonicalSubState
    );
    const interventionVisible = !item.shouldIntervene || interventionRows.some((row) => (
      isStrategyAllowedForScenario(item, row.selected_strategy_id || row.strategy_id) &&
      String(row.publish_status || row.status || '').toUpperCase().includes('PUBLISH')
    ));
    return {
      scenarioId: item.id,
      expectedSubState: item.canonicalSubState,
      expectedOverlayTags: expectedTags,
      triggerMessageId: scopedDbRuns.triggerMessageId,
      exactTriggerMatch: Boolean(scopedDbRuns.exactMatch),
      assignmentComplete,
      messageVisible,
      strategyVisible,
      interventionVisible,
      unifiedVisible,
      passed: assignmentComplete && messageVisible && strategyVisible &&
        interventionVisible && unifiedVisible
    };
  });
  const allRows = [...finalByGroup.values()].flatMap((group) => (
    ['messages.csv', 'strategy_pipeline_runs.csv', 'interventions.csv', 'unified-events.csv']
      .flatMap((filename) => exportRows(group, filename))
  ));
  const legacyFinalValues = unique(allRows
    .map((row) => row.final_sub_state_code || row.canonical_sub_state_code)
    .filter((value) => LEGACY_STATE_CODES.has(value)));
  const agentMessages = [...finalByGroup.values()].flatMap((group) => (
    exportRows(group, 'messages.csv').filter(
      (row) => String(row.role || row.sender_type || '').toLowerCase() === 'agent'
    )
  ));
  const agentMessagesClean = agentMessages.every((row) => !row.final_sub_state_code);
  const available = Boolean(finalSnapshot && finalSnapshot.auditAvailable) &&
    [...finalByGroup.values()].every((group) => (
      group.exportAudit &&
      group.exportAudit.available &&
      (group.exportAudit.missingFiles || []).length === 0
    ));
  return {
    coverageSource: 'parsed_structured_export_csv',
    auditAvailable: available,
    legacyFinalValues,
    agentMessagesClean,
    scenarios: results,
    passed: available && legacyFinalValues.length === 0 && agentMessagesClean &&
      results.length > 0 && results.every((item) => item.passed)
  };
}

function pipelineStatuses(row) {
  return unique([
    row && row.final_status,
    row && row.publish_status,
    row && row.terminal_status,
    row && row.status
  ].map(upper));
}

function pipelineIsPublished(row) {
  return pipelineStatuses(row).includes('PUBLISHED') || hasValue(row && row.published_message_id);
}

function pipelineIsTerminal(row) {
  return pipelineStatuses(row).some((status) => TERMINAL_PIPELINE_STATUSES.has(status));
}

function runMatchesScenarioState(stateScenario, run) {
  return finalSubState(run) === expectedPrimaryState(stateScenario) &&
    hasExpectedOverlayTags(stateScenario, run);
}

function buildInterventionCoverage(scenario, finalSnapshot, events) {
  const finalByGroup = snapshotGroupsByCode(finalSnapshot);
  const scenarios = scriptedScenarios(scenario).filter(
    (item) => scenarioTypeFor(item) === SCENARIO_TYPES.REQUIRED_INTERVENTION &&
      !item.expectedFailureFallback
  );
  const results = scenarios.map((item) => {
    const group = finalByGroup.get(item.groupCode);
    const sentDomains = evidenceDomains(events, item.id, {
      messageRows: dbTable(group, 'messages')
    });
    const allowedDomains = evidenceDomains(events, item.id, {
      includeRecovery: true,
      messageRows: dbTable(group, 'messages')
    });
    const scopedDbRuns = scenarioScopedPipelineRuns(
      dbTable(group, 'strategy_pipeline_runs'),
      events,
      item.id
    );
    const stateRuns = scopedDbRuns.runs.filter((run) => (
      runMatchesScenarioState(item, run) &&
      ((scopedDbRuns.exactMatch &&
        String(run.trigger_message_id || '') === String(scopedDbRuns.triggerMessageId)) ||
        evidenceAllInDomains(run, allowedDomains)) &&
      true
    ));
    const routedRuns = stateRuns.filter((run) => strategyBehaviorMatches(item, run));
    const publishedRuns = routedRuns.filter(pipelineIsPublished);
    const dbRunIds = new Set(
      publishedRuns.map((run) => String(run.id || run.pipeline_run_id))
    );
    const exportRuns = exportRows(group, 'interventions.csv').filter((row) => (
      row.canonical_sub_state_code === expectedPrimaryState(item) &&
      dbRunIds.has(String(row.strategy_pipeline_run_id)) &&
      isStrategyAllowedForScenario(item, row.selected_strategy_id || row.strategy_id) &&
      String(row.publish_status || row.status || '').toUpperCase().includes('PUBLISH')
    ));
    return {
      scenarioId: item.id,
      triggerMessageId: scopedDbRuns.triggerMessageId,
      exactTriggerMatch: Boolean(scopedDbRuns.exactMatch),
      statePassed: stateRuns.length > 0,
      routePassed: routedRuns.length > 0,
      publishedPassed: publishedRuns.length > 0 && exportRuns.length > 0,
      dbPublishedRunCount: publishedRuns.length,
      exportPublishedInterventionCount: exportRuns.length,
      passed: routedRuns.length > 0 && exportRuns.length > 0
    };
  });
  const acceptance = configuredAcceptance(scenario);
  const passedCount = results.filter((item) => item.passed).length;
  const expectedCaseCount = positiveInteger(
    acceptance.expectedPublishedCaseCount,
    results.length
  );
  const minimumPassedCaseCount = Math.min(
    results.length,
    positiveInteger(acceptance.minPublishedInterventionCases, results.length)
  );
  const caseCountPassed = results.length === expectedCaseCount;
  return {
    coverageSource: 'db_pipeline_and_interventions_export',
    requiredCaseCount: results.length,
    expectedCaseCount,
    caseCountPassed,
    passedCaseCount: passedCount,
    minimumPassedCaseCount,
    scenarios: results,
    passed: caseCountPassed && passedCount >= minimumPassedCaseCount
  };
}

function buildOptionalSupportCoverage(scenario, finalSnapshot, events) {
  const finalByGroup = snapshotGroupsByCode(finalSnapshot);
  const scenarios = scriptedScenarios(scenario).filter(
    (item) => scenarioTypeFor(item) === SCENARIO_TYPES.OPTIONAL_SUPPORT &&
      !item.expectedFailureFallback
  );
  if (!scenarios.length) return null;

  const results = scenarios.map((item) => {
    const group = finalByGroup.get(item.groupCode);
    const scoped = scenarioScopedPipelineRuns(
      dbTable(group, 'strategy_pipeline_runs'),
      events,
      item.id
    );
    const stateRuns = scoped.runs.filter((run) => runMatchesScenarioState(item, run));
    const terminalRuns = stateRuns.filter(pipelineIsTerminal);
    const publishedRuns = stateRuns.filter(pipelineIsPublished);
    const routedRuns = stateRuns.filter((run) => strategyBehaviorMatches(item, run));
    const publishedRoutePassed = publishedRuns.length === 0 || routedRuns.some(pipelineIsPublished);
    const statuses = unique(stateRuns.flatMap(pipelineStatuses));
    return {
      scenarioId: item.id,
      scenarioType: SCENARIO_TYPES.OPTIONAL_SUPPORT,
      expectedPrimaryState: expectedPrimaryState(item),
      expectedOverlayStates: expectedOverlayStates(item),
      triggerMessageId: scoped.triggerMessageId,
      exactTriggerMatch: Boolean(scoped.exactMatch),
      statePassed: stateRuns.length > 0,
      terminalPassed: terminalRuns.length > 0,
      routePassed: publishedRoutePassed,
      supportPublished: publishedRuns.length > 0,
      supportNotPublished: stateRuns.length > 0 && publishedRuns.length === 0,
      observedStatuses: statuses,
      passed: stateRuns.length > 0 && terminalRuns.length > 0 && publishedRoutePassed
    };
  });
  const publishedCaseCount = results.filter((item) => item.supportPublished).length;
  const notPublishedCaseCount = results.filter((item) => item.supportNotPublished).length;
  return {
    coverageSource: 'optional_support_state_and_terminal_outcome',
    expectedCaseCount: results.length,
    passedCaseCount: results.filter((item) => item.passed).length,
    supportPublishedCaseCount: publishedCaseCount,
    supportNotPublishedCaseCount: notPublishedCaseCount,
    scenarios: results,
    passed: results.length > 0 && results.every((item) => item.passed)
  };
}

function buildInhibitionCoverage(scenario, finalSnapshot, events) {
  const finalByGroup = snapshotGroupsByCode(finalSnapshot);
  const scenarios = scriptedScenarios(scenario).filter(
    (item) => item.inhibitionStrategyId && !item.expectedFailureFallback
  );
  const results = scenarios.map((item) => {
    const group = finalByGroup.get(item.groupCode);
    const sentDomains = evidenceDomains(events, item.id, {
      messageRows: dbTable(group, 'messages')
    });
    const allowedDomains = evidenceDomains(events, item.id, {
      includeRecovery: true,
      messageRows: dbTable(group, 'messages')
    });
    const dbRuns = dbTable(group, 'strategy_pipeline_runs').filter((run) => (
      runMatchesScenarioState(item, run) &&
      evidenceAllInDomains(run, allowedDomains) &&
      strategyBehaviorMatches(item, run)
    ));
    const dbRunIds = new Set(
      dbRuns.map((run) => String(run.id || run.pipeline_run_id))
    );
    const exportRuns = exportRows(group, 'strategy_pipeline_runs.csv').filter((row) => (
      row.canonical_sub_state_code === expectedPrimaryState(item) &&
      hasExpectedOverlayTags(item, row) &&
      dbRunIds.has(String(row.pipeline_run_id || row.id)) &&
      row.inhibition_strategy_id === item.inhibitionStrategyId &&
      String(row.publish_status || '').toUpperCase() !== 'PUBLISHED'
    ));
    return {
      scenarioId: item.id,
      inhibitionStrategyId: item.inhibitionStrategyId,
      expectedPrimaryState: expectedPrimaryState(item),
      expectedOverlayStates: expectedOverlayStates(item),
      dbSuppressedRunCount: dbRuns.length,
      exportSuppressedRunCount: exportRuns.length,
      statePassed: dbRuns.length > 0,
      cooldownSuppressed: dbRuns.some((run) => pipelineStatuses(run).some(
        (status) => status.includes('COOLDOWN')
      )),
      staleTerminal: dbRuns.some((run) => pipelineStatuses(run).includes('STALE')),
      supersededTerminal: dbRuns.some((run) => pipelineStatuses(run).includes('SUPERSEDED')),
      passed: dbRuns.length > 0 && exportRuns.length > 0
    };
  });
  const acceptance = configuredAcceptance(scenario);
  const passedCount = results.filter((item) => item.passed).length;
  const expectedCaseCount = positiveInteger(
    acceptance.expectedInhibitionCaseCount,
    results.length
  );
  const minimumPassedCaseCount = Math.min(
    results.length,
    positiveInteger(acceptance.minInhibitionCases, expectedCaseCount)
  );
  const caseCountPassed = results.length === expectedCaseCount;
  return {
    coverageSource: 'db_pipeline_and_strategy_export',
    requiredCaseCount: results.length,
    expectedCaseCount,
    caseCountPassed,
    passedCaseCount: passedCount,
    minimumPassedCaseCount,
    scenarios: results,
    passed: caseCountPassed && passedCount >= minimumPassedCaseCount
  };
}

function buildActualServerCoverage(
  scenario,
  baselineSnapshot,
  finalSnapshot,
  events = [],
  { inputIntegrity = null } = {}
) {
  const eventMarkedInputIncomplete = (events || []).some(
    (event) => event.type === 'input_incomplete'
  );
  const suppliedInputIntegrity = inputIntegrity || (
    eventMarkedInputIncomplete ? buildScriptedMessageInputIntegrity(scenario, events) : null
  );
  const plannedScriptCoverage = buildPlannedScriptCoverage(scenario);
  const messageSendCoverage = buildMessageSendCoverage(scenario, events, suppliedInputIntegrity);
  const auditAvailable = Boolean(
    baselineSnapshot && baselineSnapshot.auditAvailable &&
    finalSnapshot && finalSnapshot.auditAvailable
  );

  if (suppliedInputIntegrity && !suppliedInputIntegrity.complete) {
    return {
      coverageSource: 'db_teacher_api_export_cross_check',
      validationMode: 'input_incomplete',
      auditAvailable,
      inputIntegrity: suppliedInputIntegrity,
      plannedScriptCoverage,
      messageSendCoverage,
      actualStateCoverage: null,
      actualStrategyCoverage: null,
      modelFailureCoverage: null,
      agentLockCoverage: null,
      canonicalDbCoverage: null,
      teacherApiCoverage: null,
      exportCoverage: null,
      interventionCoverage: null,
      optionalSupportCoverage: null,
      inhibitionCoverage: null,
      coverage: {
        planned_script_coverage: plannedScriptCoverage,
        message_send_coverage: messageSendCoverage
      },
      coverageStatistics: null,
      passed: false
    };
  }

  const actualStateCoverage = buildActualStateCoverage(
    scenario,
    baselineSnapshot,
    finalSnapshot,
    events
  );
  const actualStrategyCoverage = buildActualStrategyCoverage(
    scenario,
    baselineSnapshot,
    finalSnapshot,
    events
  );
  const modelFailureCoverage = buildModelFailureCoverage(scenario, baselineSnapshot, finalSnapshot);
  const agentLockCoverage = buildAgentLockCoverage(scenario, baselineSnapshot, finalSnapshot);
  const canonicalDbCoverage = buildCanonicalDbCoverage(
    scenario,
    baselineSnapshot,
    finalSnapshot,
    events
  );
  const teacherApiCoverage = buildTeacherApiCoverage(scenario, finalSnapshot, events);
  const exportCoverage = buildExportCoverage(scenario, finalSnapshot, events);
  const interventionCoverage = buildInterventionCoverage(scenario, finalSnapshot, events);
  const optionalSupportCoverage = buildOptionalSupportCoverage(scenario, finalSnapshot, events);
  const inhibitionCoverage = buildInhibitionCoverage(scenario, finalSnapshot, events);
  const coverageStatistics = buildCoverageStatistics(
    scenario,
    baselineSnapshot,
    finalSnapshot,
    events,
    {
      interventionCoverage,
      inhibitionCoverage,
      actualStateCoverage,
      optionalSupportCoverage,
      inputIntegrity: suppliedInputIntegrity
    }
  );
  const legacyRequired = [
    actualStateCoverage,
    actualStrategyCoverage,
    modelFailureCoverage,
    agentLockCoverage
  ].filter(Boolean);
  const coverage = {
    planned_script_coverage: plannedScriptCoverage,
    message_send_coverage: messageSendCoverage,
    canonical_db_coverage: canonicalDbCoverage,
    teacher_api_coverage: teacherApiCoverage,
    export_coverage: exportCoverage,
    intervention_coverage: interventionCoverage,
    ...(optionalSupportCoverage ? { optional_support_coverage: optionalSupportCoverage } : {}),
    inhibition_coverage: inhibitionCoverage
  };
  return {
    coverageSource: 'db_teacher_api_export_cross_check',
    validationMode: 'real_coverage',
    auditAvailable,
    coverage,
    actualStateCoverage,
    actualStrategyCoverage,
    modelFailureCoverage,
    agentLockCoverage,
    plannedScriptCoverage,
    messageSendCoverage,
    canonicalDbCoverage,
    teacherApiCoverage,
    exportCoverage,
    interventionCoverage,
    optionalSupportCoverage,
    inhibitionCoverage,
    coverageStatistics,
    inputIntegrity: suppliedInputIntegrity,
    passed: auditAvailable &&
      legacyRequired.length > 0 &&
      legacyRequired.every((item) => item.passed) &&
      Object.values(coverage).every((item) => item.passed)
  };
}

module.exports = {
  TERMINAL_PIPELINE_STATUSES,
  buildActualServerCoverage,
  buildActualStateCoverage,
  buildAgentLockCoverage,
  buildCanonicalDbCoverage,
  buildCoverageStatistics,
  buildExportCoverage,
  buildInhibitionCoverage,
  buildInterventionCoverage,
  buildMessageSendCoverage,
  buildScriptedMessageInputIntegrity,
  buildModelFailureCoverage,
  buildOptionalSupportCoverage,
  buildPlannedScriptCoverage,
  buildTeacherApiCoverage,
  formatCoverageStatisticsMarkdown
};
