const assert = require('node:assert/strict');
const path = require('node:path');
const test = require('node:test');

const scenario = require('../config/six-group-strategy-coverage');
const manifest = require(path.join(__dirname, '..', '..', 'services', 'strategy_route_manifest.json'));
const {
  assertSixGroupRouterCoverage,
  buildSixGroupRouterCoverage,
  formatSixGroupRouterCoverageMarkdown,
  routeKeyForScenario
} = require('../src/sixGroupRouterCoverage');

test('Batch 2 six-group router replay reaches required coverage thresholds', () => {
  const coverage = buildSixGroupRouterCoverage(scenario, manifest);

  assert.doesNotThrow(() => assertSixGroupRouterCoverage(coverage));
  assert.equal(coverage.groupCount, 6);
  assert.equal(coverage.totalStudents, 24);
  assert.equal(coverage.scenarioCount, 30);
  assert.equal(coverage.scriptedMessageCount, 263);
  assert.equal(coverage.expectedStrategyIdCount, 28);

  assert.equal(coverage.stateCoverage.burnoutPresent, true);
  assert.equal(coverage.stateCoverage.stageAchievementPresent, true);
  assert.equal(coverage.explicitInterventionCoverage.baselinePassedCaseCount, 2);
  assert.equal(coverage.explicitInterventionCoverage.plannedCaseCount, 21);
  assert.equal(coverage.explicitInterventionCoverage.expectedCaseCount, 21);
  assert.equal(coverage.explicitInterventionCoverage.routeRequiredPassedCaseCount, 21);
  assert.equal(coverage.explicitInterventionCoverage.minimumPassedCaseCount, 18);
  assert.equal(coverage.explicitInterventionCoverage.passed, true);
  assert.equal(coverage.optionalSupportCoverage.plannedCaseCount, 3);
  assert.equal(coverage.optionalSupportCoverage.passed, true);
  assert.equal(coverage.inhibitionCoverage.plannedCaseCount, 5);
  assert.equal(coverage.inhibitionCoverage.passedCaseCount, 5);
  assert.equal(coverage.inhibitionCoverage.passed, true);
  assert.equal(coverage.strategyMapping.mismatchCount, 0);
  assert.equal(coverage.strategyMapping.noFullLibraryExposure, true);
});

test('Batch 2 keeps primary and overlay routes separate', () => {
  const byId = new Map(scenario.scriptedDiscussion.scenarios.map((item) => [item.id, item]));

  assert.equal(routeKeyForScenario(byId.get('G2-E')), 'stage_achievement');
  assert.equal(byId.get('G2-E').canonicalSubState, 'execution_progress');
  assert.deepEqual(byId.get('G2-E').expectedOverlayTags, ['stage_achievement']);

  assert.equal(routeKeyForScenario(byId.get('G3-D')), 'high_intensity_overload');
  assert.equal(routeKeyForScenario(byId.get('G6-C')), 'high_intensity_overload');

  const coverage = buildSixGroupRouterCoverage(scenario, manifest);
  const byCoverageId = new Map(coverage.scenarios.map((item) => [item.scenarioId, item]));
  const achievement = byCoverageId.get('G2-E');
  assert.deepEqual(achievement.primaryAllowedStrategyIds, ['OI-004']);
  assert.deepEqual(achievement.overlayAllowedStrategyIds, ['SS-007']);
  assert.deepEqual(achievement.runtimeAllowedStrategyIds, ['OI-004', 'SS-007']);
  assert.equal(achievement.expectedPrimaryStrategyId, 'OI-004');
  assert.equal(achievement.selectedStrategyId, 'SS-007');
  assert.equal(achievement.routeValid, true);
  assert.equal(achievement.exactPrimaryMatched, false);

  const conflict = byCoverageId.get('G3-C');
  assert.equal(conflict.expectedPrimaryStrategyId, 'ER-001');
  assert.equal(conflict.selectedStrategyId, 'EE-001');
  assert.equal(conflict.runtimeAllowedStrategyIds.includes('EE-001'), true);
  assert.equal(conflict.routeValid, true);
  assert.equal(conflict.exactPrimaryMatched, false);

  const marginalization = byCoverageId.get('G4-B');
  assert.equal(marginalization.selectedStrategyId, 'SS-002');
  assert.equal(marginalization.routeValid, true);
  assert.equal(marginalization.routeSourceVersion, manifest.version);
});

test('Batch 2 report formatter records the required acceptance numbers and route fields', () => {
  const coverage = buildSixGroupRouterCoverage(scenario, manifest);
  const markdown = formatSixGroupRouterCoverageMarkdown(coverage, {
    generatedAt: '2026-07-31T00:00:00.000Z'
  });

  assert.match(markdown, /Batch 2 最小覆盖验证通过/);
  assert.match(markdown, /21\/21/);
  assert.match(markdown, />=18\/21/);
  assert.match(markdown, /5\/5/);
  assert.match(markdown, /burnout=出现/);
  assert.match(markdown, /stage_achievement=出现/);
  assert.match(markdown, /expected_primary_strategy_id/);
  assert.match(markdown, /runtime_allowed_strategy_ids/);
  assert.match(markdown, /route_source_version/);
});

test('an invalid expected strategy still fails route coverage', () => {
  const invalidScenario = {
    ...scenario,
    scriptedDiscussion: {
      ...scenario.scriptedDiscussion,
      scenarios: scenario.scriptedDiscussion.scenarios.map((item) => (
        item.id === 'G1-A'
          ? {
              ...item,
              expected_primary_strategy_id: 'NOT-A-STRATEGY',
              expectedPrimaryStrategyId: 'NOT-A-STRATEGY',
              preferredStrategyId: 'NOT-A-STRATEGY'
            }
          : item
      ))
    }
  };
  const coverage = buildSixGroupRouterCoverage(invalidScenario, manifest);
  const failed = coverage.strategyMapping.mismatches.find((item) => item.scenarioId === 'G1-A');

  assert.ok(failed);
  assert.deepEqual(failed.invalidExpectedStrategyIds, ['NOT-A-STRATEGY']);
  assert.throws(() => assertSixGroupRouterCoverage(coverage), /G1-A/);
});
