const fs = require('fs');
const path = require('path');

const { scriptedScenarios } = require('./strategyCoverage');
const {
  SCENARIO_TYPES,
  classifyScenario,
  expectedOverlayStates,
  expectedPrimaryState,
  scenarioTypeFor
} = require('./scenarioClassification');
const {
  allManifestStrategyIds,
  expectedPrimaryStrategyIdForScenario,
  isStrategyId,
  routeForScenario,
  selectedStrategyIdForScenario,
  unique
} = require('./strategyRouteManifest');

function routeKeyForScenario(item) {
  return ((item && item.expectedOverlayTags) || [])[0] ||
    (item && item.expectedProcessState) ||
    (item && item.canonicalSubState);
}

function primaryRouteKeyForScenario(item) {
  return item && (item.expectedPrimaryState || item.expected_primary_state || item.canonicalSubState);
}

function overlayRouteKeysForScenario(item) {
  return ((item && (item.expectedOverlayStates || item.expectedOverlayTags)) || []).map(String);
}

function buildSixGroupRouterCoverage(scenario, manifest) {
  const scenarios = scriptedScenarios(scenario);
  const coverageScenarios = scenarios.filter((item) => !item.skipStrategyCoverage);
  const expectedStrategyIds = allManifestStrategyIds(manifest);
  const strategyLibrarySize = expectedStrategyIds.length;
  const acceptance = (
    scenario &&
    scenario.strategyAudit &&
    scenario.strategyAudit.interventionAcceptance
  ) || {};
  const minimumPublished = positiveInteger(
    acceptance.minPublishedInterventionCases,
    18
  );
  const expectedPublished = positiveInteger(
    acceptance.expectedPublishedCaseCount,
    coverageScenarios.filter((item) => (
      scenarioTypeFor(item) === SCENARIO_TYPES.REQUIRED_INTERVENTION
    )).length
  );
  const expectedInhibition = positiveInteger(
    acceptance.expectedInhibitionCaseCount,
    coverageScenarios.filter((item) => item.inhibitionStrategyId).length
  );

  const scenarioResults = coverageScenarios.map((item) => {
    const classification = classifyScenario(item);
    const route = routeForScenario(item, manifest);
    const routeKey = routeKeyForScenario(item);
    const declaredStrategyIds = unique(item.allowedStrategyIds);
    const invalidDeclaredStrategyIds = declaredStrategyIds.filter(
      (strategyId) => !isStrategyId(strategyId)
    );
    const expectedPrimaryStrategyId = expectedPrimaryStrategyIdForScenario(item, manifest);
    const selectedStrategyId = selectedStrategyIdForScenario(item);
    const matchedStrategyIds = declaredStrategyIds.filter((strategyId) => (
      route.combinedRuntimeAllowedStrategyIds.includes(strategyId)
    ));
    const fullLibraryExposed = route.combinedRuntimeAllowedStrategyIds.length >= strategyLibrarySize;
    const routeMode = route.routeMode;
    const expectedRouteModes = {
      [SCENARIO_TYPES.REQUIRED_INTERVENTION]: 'REQUIRED_INTERVENTION',
      [SCENARIO_TYPES.OPTIONAL_SUPPORT]: 'OPTIONAL_SUPPORT',
      [SCENARIO_TYPES.OBSERVATION_INHIBITION]: 'SUPPRESS'
    };
    const routeModeValid = routeMode === expectedRouteModes[classification.type];
    const inhibitionRoute = [
      route.primaryRoute,
      ...route.overlayRoutes.map((entry) => entry.route)
    ].find((candidate) => candidate && candidate.inhibition_strategy_id === item.inhibitionStrategyId);
    const inhibitionRouteValid = !item.inhibitionStrategyId || Boolean(inhibitionRoute);
    const selectedRouteValid = selectedStrategyId
      ? route.combinedRuntimeAllowedStrategyIds.includes(selectedStrategyId)
      : Boolean(expectedPrimaryStrategyId && route.combinedRuntimeAllowedStrategyIds.includes(expectedPrimaryStrategyId));
    const routeValid = route.routePresent &&
      routeModeValid &&
      inhibitionRouteValid &&
      selectedRouteValid &&
      !fullLibraryExposed;
    const invalidExpectedStrategyIds = unique([
      ...invalidDeclaredStrategyIds,
      ...(expectedPrimaryStrategyId && !isStrategyId(expectedPrimaryStrategyId)
        ? [expectedPrimaryStrategyId]
        : []),
      ...(selectedStrategyId && !isStrategyId(selectedStrategyId)
        ? [selectedStrategyId]
        : [])
    ]);
    const exactPrimaryMatched = Boolean(
      selectedStrategyId &&
      expectedPrimaryStrategyId &&
      selectedStrategyId === expectedPrimaryStrategyId
    );
    return {
      scenarioId: item.id,
      title: item.title,
      groupCode: item.groupCode,
      scenarioType: classification.type,
      subCategory: routeKey,
      primaryRouteKey: primaryRouteKeyForScenario(item),
      overlayRouteKeys: overlayRouteKeysForScenario(item),
      canonicalSubState: classification.primaryState,
      expectedPrimaryState: classification.primaryState,
      expectedOverlayTags: expectedOverlayStates(item),
      expectedOverlayStates: expectedOverlayStates(item),
      shouldIntervene: Boolean(item.shouldIntervene),
      inhibitionStrategyId: item.inhibitionStrategyId || null,
      routeMode,
      interventionMode: route && route.interventionMode,
      expectedStrategyIds: declaredStrategyIds,
      declaredStrategyIds,
      routeStrategyPool: route.combinedRuntimeAllowedStrategyIds,
      primaryAllowedStrategyIds: route.primaryAllowedStrategyIds,
      overlayAllowedStrategyIds: route.overlayAllowedStrategyIds,
      combinedRuntimeAllowedStrategyIds: route.combinedRuntimeAllowedStrategyIds,
      runtimeAllowedStrategyIds: route.runtimeAllowedStrategyIds,
      matchedStrategyIds,
      invalidExpectedStrategyIds,
      invalidDeclaredStrategyIds,
      fullLibraryExposed,
      expectedPrimaryStrategyId,
      selectedStrategyId,
      routeValid,
      exactPrimaryMatched,
      routeSourceVersion: route.routeSourceVersion,
      routeAccepted: routeValid,
      expected_primary_strategy_id: expectedPrimaryStrategyId,
      runtime_allowed_strategy_ids: route.runtimeAllowedStrategyIds,
      selected_strategy_id: selectedStrategyId,
      route_valid: routeValid,
      exact_primary_matched: exactPrimaryMatched,
      route_source_version: route.routeSourceVersion,
      primary_allowed_strategy_ids: route.primaryAllowedStrategyIds,
      overlay_allowed_strategy_ids: route.overlayAllowedStrategyIds,
      combined_runtime_allowed_strategy_ids: route.combinedRuntimeAllowedStrategyIds
    };
  });

  const requiredInterventionScenarios = scenarioResults.filter(
    (item) => item.scenarioType === SCENARIO_TYPES.REQUIRED_INTERVENTION && item.routeAccepted
  );
  const requiredInterventionCases = scenarioResults.filter(
    (item) => item.scenarioType === SCENARIO_TYPES.REQUIRED_INTERVENTION
  );
  const optionalSupportScenarios = scenarioResults.filter(
    (item) => item.scenarioType === SCENARIO_TYPES.OPTIONAL_SUPPORT
  );
  const optionalSupportPassed = optionalSupportScenarios.filter(
    (item) => item.routeMode === 'OPTIONAL_SUPPORT' && item.routeAccepted
  );
  const inhibitionScenarios = scenarioResults.filter(
    (item) => item.scenarioType === SCENARIO_TYPES.OBSERVATION_INHIBITION && item.inhibitionStrategyId
  );
  const passedInhibitions = inhibitionScenarios.filter((item) => item.routeAccepted);
  const stateIdentities = unique(scenarioResults.map((item) => item.subCategory));
  const mismatches = scenarioResults.filter((item) => !item.routeAccepted);
  const plannedStrategyIds = unique(
    scenarioResults.flatMap((item) => item.combinedRuntimeAllowedStrategyIds)
  );
  const missingStrategyIds = expectedStrategyIds.filter((strategyId) => (
    !plannedStrategyIds.includes(strategyId)
  ));
  const explicitPassed = requiredInterventionCases.length === expectedPublished &&
    requiredInterventionScenarios.length >= minimumPublished;
  const inhibitionPassed = inhibitionScenarios.length === expectedInhibition &&
    passedInhibitions.length === expectedInhibition;
  const statePassed = stateIdentities.includes('burnout') &&
    stateIdentities.includes('stage_achievement');
  const noFullLibraryExposure = scenarioResults.every((item) => !item.fullLibraryExposed);

  return {
    coverageSource: 'six_group_script_vs_state_strategy_router_manifest',
    manifestVersion: manifest && manifest.version,
    groupCount: scenario.groupCount,
    totalStudents: scenario.totalStudents,
    scenarioCount: scenarios.length,
    coverageScenarioCount: coverageScenarios.length,
    scriptedMessageCount: scenario.scriptedDiscussion.messages.length,
    expectedStrategyIdCount: strategyLibrarySize,
    stateIdentities,
    stateCoverage: {
      requiredStates: ['burnout', 'stage_achievement'],
      passed: statePassed,
      burnoutPresent: stateIdentities.includes('burnout'),
      stageAchievementPresent: stateIdentities.includes('stage_achievement')
    },
    explicitInterventionCoverage: {
      baselinePassedCaseCount: positiveInteger(acceptance.baselinePublishedCases, 2),
      plannedCaseCount: requiredInterventionCases.length,
      expectedCaseCount: expectedPublished,
      routeRequiredPassedCaseCount: requiredInterventionScenarios.length,
      optionalSupportCaseCount: optionalSupportScenarios.length,
      minimumPassedCaseCount: minimumPublished,
      passed: explicitPassed
    },
    requiredInterventionCoverage: {
      plannedCaseCount: requiredInterventionCases.length,
      routePassedCaseCount: requiredInterventionScenarios.length,
      expectedCaseCount: expectedPublished,
      minimumPassedCaseCount: minimumPublished,
      passed: explicitPassed
    },
    optionalSupportCoverage: {
      plannedCaseCount: optionalSupportScenarios.length,
      routePassedCaseCount: optionalSupportPassed.length,
      expectedCaseCount: optionalSupportScenarios.length,
      passedCaseCount: optionalSupportPassed.length,
      passed: optionalSupportPassed.length === optionalSupportScenarios.length
    },
    inhibitionCoverage: {
      plannedCaseCount: inhibitionScenarios.length,
      expectedCaseCount: expectedInhibition,
      passedCaseCount: passedInhibitions.length,
      passed: inhibitionPassed
    },
    strategyMapping: {
      noFullLibraryExposure,
      mismatchCount: mismatches.length,
      mismatches,
      expectedStrategyIds,
      plannedStrategyIds,
      missingStrategyIds,
      passed: noFullLibraryExposure && mismatches.length === 0 && missingStrategyIds.length === 0
    },
    scenarios: scenarioResults,
    passed: statePassed && explicitPassed &&
      optionalSupportPassed.length === optionalSupportScenarios.length &&
      inhibitionPassed &&
      noFullLibraryExposure && mismatches.length === 0
  };
}

function assertSixGroupRouterCoverage(coverage) {
  const failures = [];
  if (!coverage.stateCoverage.passed) {
    failures.push('missing required burnout/stage_achievement state identity');
  }
  if (!coverage.explicitInterventionCoverage.passed) {
    failures.push(
      `explicit route coverage ${coverage.explicitInterventionCoverage.routeRequiredPassedCaseCount}/` +
      `${coverage.explicitInterventionCoverage.plannedCaseCount} below ` +
      `${coverage.explicitInterventionCoverage.minimumPassedCaseCount}`
    );
  }
  if (!coverage.inhibitionCoverage.passed) {
    failures.push(
      `OI route coverage ${coverage.inhibitionCoverage.passedCaseCount}/` +
      `${coverage.inhibitionCoverage.expectedCaseCount}`
    );
  }
  if (!coverage.strategyMapping.passed) {
    const ids = coverage.strategyMapping.mismatches.map((item) => item.scenarioId).join(', ');
    failures.push(`strategy mapping mismatches: ${ids || 'full library exposure'}`);
  }
  if (failures.length) {
    throw new Error(`six-group router coverage failed: ${failures.join('; ')}`);
  }
}

function formatSixGroupRouterCoverageMarkdown(coverage, { generatedAt = new Date() } = {}) {
  const timestamp = generatedAt instanceof Date ? generatedAt.toISOString() : String(generatedAt);
  const explicit = coverage.explicitInterventionCoverage;
  const inhibition = coverage.inhibitionCoverage;
  const optional = coverage.scenarios
    .filter((item) => item.shouldIntervene && item.routeMode === 'OPTIONAL_SUPPORT')
    .map((item) => item.scenarioId)
    .join(', ') || '无';
  const rows = coverage.scenarios.map((item) => [
    item.scenarioId,
    item.groupCode,
    item.subCategory,
    item.canonicalSubState,
    item.expectedOverlayTags.join(', ') || '-',
    item.routeMode || 'MISSING',
    item.expectedPrimaryStrategyId || '-',
    item.primaryAllowedStrategyIds.join(', ') || '-',
    item.overlayAllowedStrategyIds.join(', ') || '-',
    item.runtimeAllowedStrategyIds.join(', ') || '-',
    item.selectedStrategyId || '-',
    item.routeValid ? '通过' : '失败',
    item.exactPrimaryMatched ? '是' : '否',
    item.routeSourceVersion || '-'
  ]);
  return [
    '# strategy_router_refactor_test_report',
    '',
    `生成时间：${timestamp}`,
    '',
    '## 结论',
    '',
    coverage.passed ? 'Batch 2 最小覆盖验证通过。' : 'Batch 2 最小覆盖验证未通过。',
    '',
    `- 六组脚本：${coverage.groupCount} 组 / ${coverage.totalStudents} 人 / ${coverage.scenarioCount} 个阶段 / ${coverage.scriptedMessageCount} 条消息。`,
    `- 状态覆盖：burnout=${coverage.stateCoverage.burnoutPresent ? '出现' : '缺失'}，stage_achievement=${coverage.stateCoverage.stageAchievementPresent ? '出现' : '缺失'}。`,
    `- 显式介入路由：从旧审计 ${explicit.baselinePassedCaseCount}/24 提升到 ${explicit.routeRequiredPassedCaseCount}/${explicit.plannedCaseCount}，验收阈值 >=${explicit.minimumPassedCaseCount}/${explicit.expectedCaseCount}。`,
    `- Optional support 阶段：${explicit.optionalSupportCaseCount} 个，保留机会型 gate，不计入确定性显式介入命中；阶段为 ${optional}。`,
    `- OI 抑制路由：${inhibition.passedCaseCount}/${inhibition.expectedCaseCount}。`,
    `- 策略池约束：未暴露完整 ${coverage.expectedStrategyIdCount} 策略库；映射失败 ${coverage.strategyMapping.mismatchCount} 个；缺少策略 ${coverage.strategyMapping.missingStrategyIds.length} 个。`,
    '',
    '## 策略映射明细',
    '',
    '| 阶段 | 组 | sub_category | canonical_state | overlay | route_mode | expected_primary_strategy_id | primary_allowed_strategy_ids | overlay_allowed_strategy_ids | runtime_allowed_strategy_ids | selected_strategy_id | route_valid | exact_primary_matched | route_source_version |',
    '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|',
    ...rows.map((row) => `| ${row.join(' | ')} |`),
    '',
    '## 验证口径',
    '',
    '- 本报告为 Batch 2 的最小本地复测：读取六组覆盖脚本与当前 `strategy_route_manifest.json`，分别验证 primary/overlay 路由、combined runtime 允许集合、selected strategy 合法性、显式介入阈值和 OI 抑制路由。',
    '- 完整浏览器/LLM 实测仍需要独立新课次、24 个登录 key、策略 Agent 开启、情绪 Agent 关闭和服务端审计导出。',
    ''
  ].join('\n');
}

function positiveInteger(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : fallback;
}

if (require.main === module) {
  const scenario = require('../config/six-group-strategy-coverage');
  const manifest = require(path.join(__dirname, '..', '..', 'services', 'strategy_route_manifest.json'));
  const outputPath = process.argv[2] ||
    path.join(__dirname, '..', '..', 'strategy_router_refactor_test_report.md');
  const coverage = buildSixGroupRouterCoverage(scenario, manifest);
  assertSixGroupRouterCoverage(coverage);
  fs.writeFileSync(outputPath, formatSixGroupRouterCoverageMarkdown(coverage), 'utf8');
  console.log(outputPath);
}

module.exports = {
  assertSixGroupRouterCoverage,
  buildSixGroupRouterCoverage,
  formatSixGroupRouterCoverageMarkdown,
  overlayRouteKeysForScenario,
  primaryRouteKeyForScenario,
  routeKeyForScenario
};
