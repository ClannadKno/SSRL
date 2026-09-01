const {
  SCENARIO_TYPES,
  classifyScenario,
  expectedOverlayStates,
  expectedPrimaryState,
  scenarioTypeFor
} = require('./scenarioClassification');
const {
  DEFAULT_MANIFEST,
  expectedPrimaryStrategyIdForScenario,
  isStrategyAllowedForScenario,
  isStrategyId,
  routeForScenario,
  selectedStrategyIdForScenario,
  unique: uniqueRouteValues
} = require('./strategyRouteManifest');

function unique(items) {
  return [...new Set((items || []).filter(Boolean).map((item) => String(item)))];
}

function listValue(value) {
  if (Array.isArray(value)) return value;
  if (value === undefined || value === null || value === '') return [];
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (!trimmed) return [];
    try {
      const parsed = JSON.parse(trimmed);
      if (Array.isArray(parsed)) return parsed;
    } catch (_error) {
      // Fall back to comma-separated legacy export values.
    }
    return trimmed.split(',').map((item) => item.trim()).filter(Boolean);
  }
  return [];
}

function scriptedScenarios(scenario) {
  const items = scenario && scenario.scriptedDiscussion && scenario.scriptedDiscussion.scenarios;
  return Array.isArray(items) ? items : [];
}

function scenarioStateIdentity(item) {
  const primaryState = expectedPrimaryState(item);
  const overlays = expectedOverlayStates(item);
  return overlays.length ? `${primaryState}|${overlays.join('|')}` : primaryState;
}

function expectedStrategyIdsForScenario(scenario) {
  const script = scenario && scenario.scriptedDiscussion;
  if (!script) return [];
  if (Array.isArray(script.expectedStrategyIds)) {
    return unique(script.expectedStrategyIds);
  }
  return uniqueRouteValues(
    scriptedScenarios(scenario).flatMap((item) => routeForScenario(item).runtimeAllowedStrategyIds)
  );
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

function messageKey(message) {
  return `${message.studentId || ''}\n${Number(message.seq || 0)}`;
}

function buildScriptedStrategyCoverage(scenario, events) {
  const stateScenarios = scriptedScenarios(scenario).filter((item) => !item.skipStrategyCoverage);
  if (!stateScenarios.length) return null;

  const messages = scenario.scriptedDiscussion.messages || [];
  const successfulKeys = new Set(
    (events || [])
      .filter((event) => event.type === 'message_success' && String(event.kind || '').startsWith('script:'))
      .map(messageKey)
  );
  const expectedStrategyIds = expectedStrategyIdsForScenario(scenario);
  const plannedStrategyIds = uniqueRouteValues(
    stateScenarios.flatMap((item) => routeForScenario(item).runtimeAllowedStrategyIds)
  );
  const scenarioResults = stateScenarios.map((item) => {
    const route = routeForScenario(item);
    const plannedMessages = messages.filter((message) => message.scenarioId === item.id);
    const successfulMessages = plannedMessages.filter((message) => successfulKeys.has(messageKey(message)));
    const passed = plannedMessages.length > 0 && successfulMessages.length === plannedMessages.length;
    return {
      scenarioId: item.id,
      title: item.title,
      groupCode: item.groupCode,
      scenarioType: scenarioTypeFor(item),
      expectedPrimaryState: expectedPrimaryState(item),
      expectedOverlayStates: expectedOverlayStates(item),
      canonicalSubState: expectedPrimaryState(item),
      shouldIntervene: Boolean(item.shouldIntervene),
      inhibitionStrategyId: item.inhibitionStrategyId || null,
      selectedStrategyId: item.selectedStrategyId || null,
      allowedStrategyIds: unique(item.allowedStrategyIds),
      runtimeAllowedStrategyIds: route.runtimeAllowedStrategyIds,
      expectedPrimaryStrategyId: expectedPrimaryStrategyIdForScenario(item),
      routeSourceVersion: route.routeSourceVersion,
      plannedMessageCount: plannedMessages.length,
      successfulMessageCount: successfulMessages.length,
      passed
    };
  });
  const successfulStrategyIds = unique(
    scenarioResults.filter((item) => item.passed).flatMap((item) => item.runtimeAllowedStrategyIds)
  );
  const missingPlannedStrategyIds = expectedStrategyIds.filter((item) => !plannedStrategyIds.includes(item));
  const missingSuccessfulStrategyIds = expectedStrategyIds.filter((item) => !successfulStrategyIds.includes(item));

  return {
    coverageSource: 'script_markers_and_declared_routes_only',
    provesServerRouting: false,
    expectedSubStates: unique(stateScenarios.map((item) => item.canonicalSubState)),
    plannedSubStates: unique(messages.map((message) => message.state)),
    expectedStrategyIds,
    plannedStrategyIds,
    successfulStrategyIds,
    missingPlannedStrategyIds,
    missingSuccessfulStrategyIds,
    scenarios: scenarioResults,
    passed: scenarioResults.every((item) => item.passed) &&
      missingPlannedStrategyIds.length === 0 &&
      missingSuccessfulStrategyIds.length === 0
  };
}

function assertDryRunStrategyCoverage(scenario) {
  const script = scenario && scenario.scriptedDiscussion;
  const stateScenarios = scriptedScenarios(scenario);
  if (!stateScenarios.length) return;

  const ids = unique(stateScenarios.map((item) => item.id));
  const groups = unique(stateScenarios.map((item) => item.groupCode));
  const states = unique(stateScenarios.map(scenarioStateIdentity));
  if (ids.length !== stateScenarios.length) throw new Error('State scenario IDs must be unique.');
  if (script.requireIsolatedGroups && groups.length !== stateScenarios.length) {
    throw new Error('Each state scenario must use an isolated discussion group.');
  }
  if (script.requireIsolatedDiscussions) {
    const suite = scenario.stateSuite || {};
    if (groups.length !== 1 || groups[0] !== suite.groupCode) {
      throw new Error('Sequential state scenarios must all use the configured four-person group.');
    }
    if (suite.isolation !== 'experiment_session') {
      throw new Error('Sequential state scenarios must isolate every round with a new experiment session.');
    }
  }
  if (!script.allowRepeatedCanonicalSubStates && states.length !== stateScenarios.length) {
    throw new Error('Each scripted report scenario must cover a distinct canonical sub-state.');
  }

  const messages = script.messages || [];
  for (const item of stateScenarios) {
    if (!item.id || !item.groupCode || !item.canonicalSubState) {
      throw new Error('Each state scenario requires id, groupCode, and canonicalSubState.');
    }
    const classification = classifyScenario(item);
    if (!classification.primaryState) {
      throw new Error(`${item.id} requires an expected primary state.`);
    }
    if (!item.expectedFailureFallback &&
        classification.requiredIntervention && item.shouldIntervene !== true) {
      throw new Error(`${item.id} required_intervention scenarios must set shouldIntervene=true.`);
    }
    if (!item.expectedFailureFallback &&
        classification.observationInhibition && item.shouldIntervene === true) {
      throw new Error(`${item.id} observation_inhibition scenarios must not require intervention.`);
    }
    const invalidStrategies = (item.allowedStrategyIds || [])
      .filter((value) => !isStrategyId(value));
    if (invalidStrategies.length) {
      throw new Error(`${item.id} has invalid strategy IDs: ${invalidStrategies.join(', ')}`);
    }
    if (item.inhibitionStrategyId && item.shouldIntervene) {
      throw new Error(`${item.id} cannot both intervene and use an inhibition strategy.`);
    }
    if (item.inhibitionStrategyId && !isStrategyAllowedForScenario(item, item.inhibitionStrategyId)) {
      throw new Error(`${item.id} inhibition strategy is outside the authoritative runtime route.`);
    }
    if (item.selectedStrategyId && !isStrategyAllowedForScenario(item, item.selectedStrategyId)) {
      throw new Error(`${item.id} selected strategy is outside the authoritative runtime route.`);
    }

    const itemMessages = messages.filter((message) => message.scenarioId === item.id);
    if (!itemMessages.length) throw new Error(`${item.id} has no scripted messages.`);
    const wrongGroup = itemMessages.filter((message) => message.groupCode !== item.groupCode);
    if (wrongGroup.length) throw new Error(`${item.id} contains messages assigned to another group.`);
    const wrongState = itemMessages.filter((message) => message.state !== item.canonicalSubState);
    if (wrongState.length) throw new Error(`${item.id} contains messages with another state label.`);
    const memberNumbers = unique(itemMessages.map((message) => {
      const match = String(message.studentId || '').match(/-M(\d+)$/i);
      return match ? match[1] : null;
    }));
    const minimumParticipants = Math.max(1, Number(item.minimumParticipants || 2));
    if (memberNumbers.length < minimumParticipants) {
      throw new Error(`${item.id} must include at least ${minimumParticipants} group members.`);
    }
  }

  const expectedStrategyIds = expectedStrategyIdsForScenario(scenario);
  const plannedStrategyIds = uniqueRouteValues(
    stateScenarios.flatMap((item) => routeForScenario(item).runtimeAllowedStrategyIds)
  );
  const missing = expectedStrategyIds.filter((item) => !plannedStrategyIds.includes(item));
  if (script.mustCoverAllExpectedStrategies && missing.length) {
    throw new Error(`Scripted scenarios do not cover expected strategies: ${missing.join(', ')}`);
  }
}

function scenarioTriggerMessageId(events, scenarioId) {
  const started = (events || []).filter((event) => (
    ['expected_intervention_started', 'expected_no_intervention_started'].includes(event.type) &&
    (event.scenarioId || event.scenario_id) === scenarioId &&
    event.messageId !== undefined && event.messageId !== null
  ));
  const last = started[started.length - 1];
  return last ? String(last.messageId || last.triggerMessageId || last.trigger_message_id) : null;
}

function scenarioScopedPipelineRuns(runs, events, scenarioId) {
  const triggerMessageId = scenarioTriggerMessageId(events, scenarioId);
  if (!triggerMessageId) return { triggerMessageId: null, runs };
  const exact = runs.filter((run) => (
    run.trigger_message_id !== undefined &&
    run.trigger_message_id !== null &&
    String(run.trigger_message_id) === triggerMessageId
  ));
  return {
    triggerMessageId,
    runs: exact.length ? exact : runs,
    exactMatch: exact.length > 0,
  };
}

function buildActualStrategyCoverage(scenario, baselineSnapshot, finalSnapshot, events = []) {
  const stateScenarios = scriptedScenarios(scenario).filter((item) => !item.skipStrategyCoverage);
  if (!stateScenarios.length) return null;

  const baselineByGroup = snapshotGroupsByCode(baselineSnapshot);
  const finalByGroup = snapshotGroupsByCode(finalSnapshot);
  const requireClean = Boolean(scenario.strategyAudit && scenario.strategyAudit.requireCleanContext);
  const dirtyGroups = [];

  for (const item of stateScenarios) {
    const baseline = baselineByGroup.get(item.groupCode);
    const studentMessages = ((baseline && baseline.audit && baseline.audit.message_timeline) || [])
      .filter((message) => String(message.role || message.resolved_role || '').toLowerCase() === 'student');
    if (studentMessages.length) {
      dirtyGroups.push({ groupCode: item.groupCode, studentMessageCount: studentMessages.length });
    }
  }

  const scenarioResults = stateScenarios.map((item) => {
    const classification = classifyScenario(item);
    const route = routeForScenario(item, DEFAULT_MANIFEST);
    const expectedPrimaryStrategyId = expectedPrimaryStrategyIdForScenario(item, DEFAULT_MANIFEST);
    const baseline = baselineByGroup.get(item.groupCode);
    const final = finalByGroup.get(item.groupCode);
    const baselineRunIds = new Set(
      (((baseline && baseline.audit) || {}).strategy_pipeline_runs || []).map(runIdentity)
    );
    const allFinalRuns = (((final && final.audit) || {}).strategy_pipeline_runs || []);
    const newRuns = allFinalRuns.filter((run) => !baselineRunIds.has(runIdentity(run)));
    const expectedTags = classification.overlayStates;
    const scopedRuns = scenarioScopedPipelineRuns(newRuns, events, item.id);
    const matchingRuns = scopedRuns.runs.filter((run) => (
      run.canonical_sub_state_code === classification.primaryState &&
      expectedTags.every((tag) => strategyRunOverlayTags(run).includes(tag))
    ));
    const observedCandidateStrategyIds = unique(matchingRuns.flatMap((run) => run.strategy_candidate_ids || []));
    const observedRoutedStrategyIds = unique(matchingRuns.flatMap(strategyIdsFromRun));
    const candidateRouteAllowed = classification.type !== SCENARIO_TYPES.REQUIRED_INTERVENTION;
    const observedStrategyIds = candidateRouteAllowed
      ? unique([...observedRoutedStrategyIds, ...observedCandidateStrategyIds])
      : observedRoutedStrategyIds;
    const observedSelectedStrategyIds = validStrategyIds(matchingRuns.map((run) => run.selected_strategy_id));
    const observedInhibitionStrategyIds = validStrategyIds(matchingRuns.map((run) => run.inhibition_strategy_id));
    const observedRuntimeStrategyIds = unique([
      ...observedRoutedStrategyIds,
      ...observedCandidateStrategyIds
    ].filter((strategyId) => route.runtimeAllowedStrategyIds.includes(strategyId)));
    const observedSubStates = unique(newRuns.map((run) => run.canonical_sub_state_code));
    const allowedStrategyIds = unique(item.allowedStrategyIds);
    const missingStrategyIds = allowedStrategyIds
      .filter((strategyId) => !observedStrategyIds.includes(strategyId));
    const hasAllowedStrategy = observedRuntimeStrategyIds.length > 0;
    const observedShouldIntervene = matchingRuns
      .map((run) => parseBoolean(run.should_intervene))
      .filter((value) => value !== null);
    const shouldIntervenePassed = classification.type === SCENARIO_TYPES.OPTIONAL_SUPPORT
      ? matchingRuns.length > 0
      : observedShouldIntervene.some(
        (value) => value === (classification.type === SCENARIO_TYPES.REQUIRED_INTERVENTION)
      );
    const inhibitionPassed = !item.inhibitionStrategyId ||
      observedInhibitionStrategyIds.includes(item.inhibitionStrategyId);
    const expectedSelectedStrategyId = item.selectedStrategyId || item.selected_strategy_id || null;
    const exactStrategyRequired = Boolean(
      item.exactStrategyRequired ||
      item.exact_strategy_required ||
      expectedSelectedStrategyId
    );
    const selectedPassed = !exactStrategyRequired ||
      observedSelectedStrategyIds.includes(String(expectedSelectedStrategyId));
    const selfRegulationPassed = item.detectedSelfRegulation === undefined || matchingRuns.some(
      (run) => parseBoolean(run.detected_self_regulation) === Boolean(item.detectedSelfRegulation)
    );
    const expectedFinalStatuses = classification.type === SCENARIO_TYPES.REQUIRED_INTERVENTION
      ? [
          'PUBLISHED', 'SUPPRESSED', 'SUPPRESSED_COOLDOWN', 'SKIPPED', 'FAILED',
          'STALE', 'SUPERSEDED', 'PENDING_DECISION_GATE'
        ]
      : classification.type === SCENARIO_TYPES.OPTIONAL_SUPPORT
        ? [
            'PUBLISHED', 'NOT_PUBLISHED', 'SUPPRESSED', 'SUPPRESSED_COOLDOWN',
            'SKIPPED', 'FAILED', 'STALE', 'SUPERSEDED', 'PENDING_DECISION_GATE'
          ]
        : ['SUPPRESSED', 'SUPPRESSED_COOLDOWN', 'SKIPPED', 'FAILED', 'STALE', 'SUPERSEDED', 'PENDING_STAGE2'];
    const observedFinalStatuses = unique(matchingRuns.map((run) => String(run.final_status || '').toUpperCase()));
    const terminalPassed = expectedFinalStatuses.some((status) => observedFinalStatuses.includes(status));
    const routeValidity = matchingRuns.map((run) => runRouteValidity(
      item,
      classification,
      route,
      run
    ));
    const routeValid = item.expectedFailureFallback
      ? true
      : route.routePresent && routeValidity.some(Boolean);
    const routeRequired = classification.type === SCENARIO_TYPES.REQUIRED_INTERVENTION ||
      Boolean(item.inhibitionStrategyId);
    const optionalRouteAllowed = classification.type !== SCENARIO_TYPES.OPTIONAL_SUPPORT ||
      observedRuntimeStrategyIds.length === 0 || routeValid;
    const passed = matchingRuns.length > 0 && shouldIntervenePassed && inhibitionPassed &&
      selectedPassed && selfRegulationPassed && terminalPassed &&
      routeValid &&
      (routeRequired ? hasAllowedStrategy : optionalRouteAllowed);

    return {
      scenarioId: item.id,
      title: item.title,
      groupCode: item.groupCode,
      scenarioType: classification.type,
      expectedSubState: classification.primaryState,
      expectedPrimaryState: classification.primaryState,
      expectedOverlayTags: expectedTags,
      expectedOverlayStates: expectedTags,
      observedSubStates,
      observedOverlayTags: unique(newRuns.flatMap(strategyRunOverlayTags)),
      expectedShouldIntervene: Boolean(item.shouldIntervene),
      shouldIntervenePassed,
      expectedInhibitionStrategyId: item.inhibitionStrategyId || null,
      observedInhibitionStrategyIds,
      inhibitionPassed,
      expectedSelectedStrategyId,
      observedSelectedStrategyIds,
      selectedPassed,
      exactStrategyRequired,
      expectedPrimaryStrategyId,
      selectedStrategyId: observedSelectedStrategyIds[0] || null,
      routeMode: route.routeMode,
      primaryAllowedStrategyIds: route.primaryAllowedStrategyIds,
      overlayAllowedStrategyIds: route.overlayAllowedStrategyIds,
      combinedRuntimeAllowedStrategyIds: route.combinedRuntimeAllowedStrategyIds,
      runtimeAllowedStrategyIds: route.runtimeAllowedStrategyIds,
      observedRuntimeStrategyIds,
      routeValid,
      exactPrimaryMatched: Boolean(
        expectedPrimaryStrategyId && observedSelectedStrategyIds.includes(expectedPrimaryStrategyId)
      ),
      routeSourceVersion: route.routeSourceVersion,
      expected_primary_strategy_id: expectedPrimaryStrategyId,
      runtime_allowed_strategy_ids: route.runtimeAllowedStrategyIds,
      selected_strategy_id: observedSelectedStrategyIds[0] || null,
      route_valid: routeValid,
      exact_primary_matched: Boolean(
        expectedPrimaryStrategyId && observedSelectedStrategyIds.includes(expectedPrimaryStrategyId)
      ),
      route_source_version: route.routeSourceVersion,
      expectedSelfRegulation: item.detectedSelfRegulation === undefined ? null : Boolean(item.detectedSelfRegulation),
      selfRegulationPassed,
      expectedFinalStatuses,
      observedFinalStatuses,
      terminalPassed,
      routeRequired,
      allowedStrategyIds,
      observedCandidateStrategyIds,
      observedRoutedStrategyIds,
      observedStrategyIds,
      missingStrategyIds,
      hasAllowedStrategy,
      triggerMessageId: scopedRuns.triggerMessageId,
      exactTriggerMatch: Boolean(scopedRuns.exactMatch),
      newPipelineRunCount: newRuns.length,
      matchingPipelineRunCount: matchingRuns.length,
      passed
    };
  });

  const expectedStrategyIds = expectedStrategyIdsForScenario(scenario);
  const observedStrategyIds = unique(scenarioResults.flatMap((item) => item.observedStrategyIds));
  const missingStrategyIds = expectedStrategyIds.filter((item) => !observedStrategyIds.includes(item));
  const requireAllExpectedStrategies = Boolean(
    scenario && scenario.scriptedDiscussion && scenario.scriptedDiscussion.mustCoverAllExpectedStrategies
  );
  const contextIsolationPassed = !requireClean || (
    Boolean(baselineSnapshot && baselineSnapshot.sessionId) && dirtyGroups.length === 0
  );
  const acceptance = configuredAcceptance(scenario);
  const requiredResults = scenarioResults.filter(
    (item) => item.scenarioType === SCENARIO_TYPES.REQUIRED_INTERVENTION
  );
  const optionalResults = scenarioResults.filter(
    (item) => item.scenarioType === SCENARIO_TYPES.OPTIONAL_SUPPORT
  );
  const inhibitionResults = scenarioResults.filter(
    (item) => item.scenarioType === SCENARIO_TYPES.OBSERVATION_INHIBITION &&
      item.expectedInhibitionStrategyId
  );
  const expectedPublishedCaseCount = positiveInteger(
    acceptance.expectedPublishedCaseCount,
    requiredResults.length
  );
  const minimumPublishedCaseCount = Math.min(
    requiredResults.length,
    positiveInteger(acceptance.minPublishedInterventionCases, requiredResults.length)
  );
  const publishedPassedCaseCount = requiredResults.filter((item) => item.passed).length;
  const expectedInhibitionCaseCount = positiveInteger(
    acceptance.expectedInhibitionCaseCount,
    inhibitionResults.length
  );
  const minimumInhibitionCaseCount = Math.min(
    inhibitionResults.length,
    positiveInteger(acceptance.minInhibitionCases, expectedInhibitionCaseCount)
  );
  const inhibitionPassedCaseCount = inhibitionResults.filter((item) => item.passed).length;
  const optionalSupportPassedCaseCount = optionalResults.filter((item) => item.passed).length;
  const acceptancePassed = requiredResults.length === expectedPublishedCaseCount &&
    publishedPassedCaseCount >= minimumPublishedCaseCount &&
    inhibitionResults.length === expectedInhibitionCaseCount &&
    inhibitionPassedCaseCount >= minimumInhibitionCaseCount &&
    optionalSupportPassedCaseCount === optionalResults.length;
  return {
    auditAvailable: Boolean(finalSnapshot && finalSnapshot.sessionId),
    sessionId: finalSnapshot && finalSnapshot.sessionId,
    contextIsolationPassed,
    dirtyGroups,
    expectedStrategyIds,
    observedStrategyIds,
    missingStrategyIds,
    scenarios: scenarioResults,
    coverageSource: 'teacher_agent_audit_strategy_pipeline_runs',
    routeSourceVersion: DEFAULT_MANIFEST.version,
    routeAcceptance: (
      'selected_strategy_id must be in the authoritative primary+overlay runtime route; ' +
      'expected_primary_strategy_id is diagnostic unless exact_strategy_required is set; ' +
      'candidate-only is accepted only for non-intervening states without OI'
    ),
    thresholdAcceptance: {
      expectedPublishedCaseCount,
      minimumPublishedCaseCount,
      publishedPassedCaseCount,
      expectedInhibitionCaseCount,
      minimumInhibitionCaseCount,
      inhibitionPassedCaseCount,
      requiredInterventionCaseCount: requiredResults.length,
      optionalSupportCaseCount: optionalResults.length,
      optionalSupportPassedCaseCount,
      passed: acceptancePassed
    },
    requireAllExpectedStrategies,
    passed: Boolean(finalSnapshot && finalSnapshot.sessionId) && contextIsolationPassed &&
      acceptancePassed &&
      (!requireAllExpectedStrategies || missingStrategyIds.length === 0)
  };
}

function snapshotGroupsByCode(snapshot) {
  return new Map(((snapshot && snapshot.groups) || []).map((item) => [item.groupCode, item]));
}

function runIdentity(run) {
  return String(run.pipeline_run_id || run.id || run.run_uuid || '');
}

function strategyIdsFromRun(run) {
  return validStrategyIds([
    ...(run.supporting_strategy_ids || []),
    run.selected_strategy_id,
    run.inhibition_strategy_id
  ]);
}

function strategyRunOverlayTags(run) {
  return unique(listValue(
    run && (
      run.secondary_sub_state_tags ||
      run.secondary_sub_state_tags_json ||
      run.secondary_tags ||
      run.state_overlays
    )
  ));
}

function runRouteValidity(item, classification, route, run) {
  if (item.expectedFailureFallback) return true;
  if (!route.routePresent) return false;
  const runtimeAllowed = new Set(route.runtimeAllowedStrategyIds);
  if (item.inhibitionStrategyId) {
    return String(run.inhibition_strategy_id || '') === String(item.inhibitionStrategyId) &&
      runtimeAllowed.has(String(item.inhibitionStrategyId));
  }
  const selectedStrategyId = String(
    run.selected_strategy_id || run.inhibition_strategy_id || ''
  ).trim();
  const published = [run.publish_status, run.final_status]
    .some((value) => String(value || '').toUpperCase() === 'PUBLISHED');
  if (classification.type === SCENARIO_TYPES.REQUIRED_INTERVENTION || published) {
    return Boolean(selectedStrategyId) && runtimeAllowed.has(selectedStrategyId);
  }
  const routeIds = unique([
    ...strategyIdsFromRun(run),
    ...validStrategyIds(run.strategy_candidate_ids || [])
  ]);
  return routeIds.length === 0 || routeIds.some((strategyId) => runtimeAllowed.has(strategyId));
}

function validStrategyIds(items) {
  return unique(items).filter((item) => /^(?:EA|EE|ER|SS|OI)-\d{3}$/.test(item));
}

function parseBoolean(value) {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  const normalized = String(value || '').trim().toLowerCase();
  if (['true', '1', 'yes', '是'].includes(normalized)) return true;
  if (['false', '0', 'no', '否'].includes(normalized)) return false;
  return null;
}

module.exports = {
  assertDryRunStrategyCoverage,
  buildActualStrategyCoverage,
  buildScriptedStrategyCoverage,
  expectedStrategyIdsForScenario,
  scenarioScopedPipelineRuns,
  scenarioTriggerMessageId,
  scenarioStateIdentity,
  scriptedScenarios
};
