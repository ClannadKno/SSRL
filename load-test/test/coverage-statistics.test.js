const assert = require('node:assert/strict');
const test = require('node:test');

const {
  buildCoverageStatistics,
  buildInhibitionCoverage,
  buildOptionalSupportCoverage,
  formatCoverageStatisticsMarkdown
} = require('../src/serverCoverage');
const { buildActualStrategyCoverage: buildActualStrategyRouteCoverage } = require('../src/strategyCoverage');
const { Metrics } = require('../src/metrics');

function snapshot(pipelines, interventions, messages, latencyEvents = []) {
  return {
    sessionId: 42,
    auditAvailable: true,
    groups: [{
      groupCode: 'G01',
      dbAudit: {
        tables: {
          strategy_pipeline_runs: pipelines,
          intervention_runs: interventions,
          messages,
          strategy_pipeline_latency_events: latencyEvents
        }
      }
    }]
  };
}

function pipeline(id, triggerMessageId, publishedMessageId) {
  return {
    id,
    run_uuid: `run-${id}`,
    group_id: 1,
    session_id: 42,
    discussion_id: 77,
    should_intervene: 1,
    input_start_sequence: triggerMessageId,
    stage3_started_at: '2026-07-31 12:00:00',
    stage3_completed_at: '2026-07-31 12:00:01',
    stage3_status: 'SUCCEEDED',
    publish_status: 'PUBLISHED',
    final_status: 'PUBLISHED',
    published_message_id: publishedMessageId
  };
}

test('coverage statistics keep publication, boundary, and scenario counts separate', () => {
  const scenario = {
    scriptedDiscussion: {
      scenarios: [
        { id: 'S1', shouldIntervene: true },
        { id: 'S2', shouldIntervene: true },
        { id: 'S3', shouldIntervene: true }
      ]
    }
  };
  const pipelines = [
    pipeline(1, 11, 101),
    pipeline(2, 12, 102),
    pipeline(3, 13, 103)
  ];
  const interventions = [
    { id: 201, intervention_run_id: 201, strategy_pipeline_run_id: 1, message_id: 101 },
    { id: 202, intervention_run_id: 202, strategy_pipeline_run_id: 2, message_id: 102 },
    { id: 203, intervention_run_id: 203, strategy_pipeline_run_id: 3, message_id: 103 }
  ];
  const messages = [
    { id: 11, role: 'student' },
    { id: 12, role: 'student' },
    { id: 13, role: 'student' },
    { id: 101, role: 'agent', intervention_run_id: 201 },
    { id: 102, role: 'agent', intervention_run_id: 202 },
    { id: 103, role: 'agent', intervention_run_id: 203 },
    // This message is time-adjacent but has no published pipeline association.
    { id: 104, role: 'agent', created_at: '2026-07-31 12:00:02' }
  ];
  const baseline = snapshot([], [], []);
  const final = snapshot(pipelines, interventions, messages);
  const events = [
    {
      type: 'expected_intervention_completed',
      scenarioId: 'S1',
      messageId: 11,
      agentMessageId: 101
    },
    {
      type: 'expected_intervention_completed',
      scenarioId: 'S2',
      messageId: 12,
      agentMessageId: 102
    },
    // A duplicate boundary observation must not inflate the count.
    {
      type: 'expected_intervention_completed',
      scenarioId: 'S2',
      messageId: 12,
      agentMessageId: 102
    }
  ];
  const statistics = buildCoverageStatistics(
    scenario,
    baseline,
    final,
    events,
    {
      interventionCoverage: {
        expectedCaseCount: 3,
        passedCaseCount: 1,
        scenarios: [{ scenarioId: 'S1', passed: true }]
      },
      inhibitionCoverage: {
        expectedCaseCount: 0,
        passedCaseCount: 0,
        scenarios: []
      }
    }
  );

  assert.equal(statistics.expected_intervention_cases, 3);
  assert.equal(statistics.stage2_should_intervene_count, 3);
  assert.equal(statistics.stage3_started_count, 3);
  assert.equal(statistics.stage3_succeeded_count, 3);
  assert.equal(statistics.stage3_failed_count, 0);
  assert.equal(statistics.publish_attempted_count, 3);
  assert.equal(statistics.published_pipeline_count, 3);
  assert.equal(statistics.visible_agent_message_count, 3);
  assert.equal(statistics.expected_boundary_completed_count, 2);
  assert.equal(statistics.scenario_coverage_passed_count, 1);
  assert.equal(statistics.inhibition_case_passed_count, 0);
  assert.deepEqual(statistics.associations.published_message_ids.sort(), ['101', '102', '103']);
  assert.equal(statistics.unlinked_agent_message_count, 1);

  const report = formatCoverageStatisticsMarkdown(statistics);
  assert.match(report, /实际发布：3/);
  assert.match(report, /学生可见 Agent 消息：3/);
  assert.match(report, /锁—消息—解锁完整边界：2/);
  assert.match(report, /严格场景覆盖通过：1\/3/);

  const metrics = new Metrics({ runId: 'coverage-fixture', scenario });
  metrics.actualServerCoverage = () => ({
    validationMode: 'real_coverage',
    auditAvailable: true,
    coverageStatistics: statistics,
    passed: false
  });
  const summary = metrics.summary();
  assert.equal(summary.coverage_statistics.published_pipeline_count, 3);
  assert.match(summary.coverage_statistics_report, /实际发布：3/);
  assert.match(summary.coverage_statistics_report, /严格场景覆盖通过：1\/3/);
});

test('coverage statistics read bounded publish gate telemetry from the audit snapshot', () => {
  const scenario = {
    scriptedDiscussion: {
      scenarios: [{ id: 'S1', shouldIntervene: true }]
    }
  };
  const pipelines = [pipeline(1, 11, 101), pipeline(2, 12, 102)];
  const latencyEvents = [
    { pipeline_run_id: 1, event: 'publish_started', details_json: '{}' },
    {
      pipeline_run_id: 1,
      event: 'publish_gate_evaluated',
      details_json: JSON.stringify({ publish_gate_allowed: true, publish_gate_result: 'allowed' })
    },
    { pipeline_run_id: 2, event: 'publish_started', details_json: '{}' },
    {
      pipeline_run_id: 2,
      event: 'publish_gate_evaluated',
      details_json: JSON.stringify({ publish_gate_allowed: false, publish_gate_result: 'blocked' })
    },
    { pipeline_run_id: 1, event: 'message_committed', details_json: '{}' }
  ];
  const statistics = buildCoverageStatistics(
    scenario,
    snapshot([], [], []),
    snapshot(pipelines, [], [], latencyEvents),
    []
  );

  assert.equal(statistics.publish_attempt_event_count, 2);
  assert.equal(statistics.publish_attempted_pipeline_count, 2);
  assert.equal(statistics.publish_gate_passed_pipeline_count, 1);
  assert.equal(statistics.publish_runtime_gate_blocked_count, 1);
  assert.equal(statistics.publisher_committed_event_count, 1);
  assert.equal(statistics.publisher_committed_pipeline_count, 1);
});

test('standard optional support may finish without an Agent publication', () => {
  const scenario = {
    scriptedDiscussion: {
      scenarios: [{
        id: 'STANDARD-OPTIONAL',
        groupCode: 'G01',
        scenarioType: 'optional_support',
        expectedPrimaryState: 'standard',
        expectedOverlayStates: [],
        allowedStrategyIds: ['SS-001'],
        shouldIntervene: true
      }]
    }
  };
  const coverage = buildOptionalSupportCoverage(
    scenario,
    snapshot([
      {
        id: 1,
        canonical_sub_state_code: 'standard',
        should_intervene: 0,
        publish_status: 'NOT_PUBLISHED',
        final_status: 'SUPPRESSED'
      }
    ], [], []),
    []
  );

  assert.equal(coverage.expectedCaseCount, 1);
  assert.equal(coverage.supportPublishedCaseCount, 0);
  assert.equal(coverage.supportNotPublishedCaseCount, 1);
  assert.equal(coverage.scenarios[0].statePassed, true);
  assert.equal(coverage.passed, true);
});

test('execution progress inhibition is a normal suppressed outcome', () => {
  const scenario = {
    scriptedDiscussion: {
      scenarios: [{
        id: 'EXECUTION-OI',
        groupCode: 'G01',
        scenarioType: 'observation_inhibition',
        expectedPrimaryState: 'execution_progress',
        expectedOverlayStates: [],
        canonicalSubState: 'execution_progress',
        inhibitionStrategyId: 'OI-004',
        allowedStrategyIds: ['OI-004'],
        shouldIntervene: false
      }]
    }
  };
  const final = snapshot([
    {
      id: 1,
      canonical_sub_state_code: 'execution_progress',
      should_intervene: 0,
      inhibition_strategy_id: 'OI-004',
      publish_status: 'SUPPRESSED',
      final_status: 'SUPPRESSED',
      evidence_message_ids: [1]
    }
  ], [], []);
  final.groups[0].exportAudit = {
    available: true,
    files: {
      'strategy_pipeline_runs.csv': [{
        pipeline_run_id: 1,
        canonical_sub_state_code: 'execution_progress',
        inhibition_strategy_id: 'OI-004',
        publish_status: 'SUPPRESSED'
      }]
    }
  };
  const coverage = buildInhibitionCoverage(scenario, final, [
    { type: 'message_success', scenarioId: 'EXECUTION-OI', messageId: 1, seq: 1 }
  ]);

  assert.equal(coverage.scenarios[0].statePassed, true);
  assert.equal(coverage.scenarios[0].cooldownSuppressed, false);
  assert.equal(coverage.passed, true);
});

test('a legal backup strategy is accepted as the selected route', () => {
  const scenario = {
    strategyAudit: { requireCleanContext: false },
    scriptedDiscussion: {
      scenarios: [{
        id: 'BACKUP-ROUTE',
        groupCode: 'G01',
        scenarioType: 'required_intervention',
        canonicalSubState: 'confusion',
        expectedPrimaryState: 'confusion',
        expectedOverlayStates: [],
        allowedStrategyIds: ['EA-001', 'ER-005'],
        shouldIntervene: true
      }]
    }
  };
  const coverage = buildActualStrategyRouteCoverage(
    scenario,
    { sessionId: 42, groups: [{ groupCode: 'G01', audit: { strategy_pipeline_runs: [] } }] },
    {
      sessionId: 42,
      groups: [{
        groupCode: 'G01',
        audit: {
          strategy_pipeline_runs: [{
            id: 1,
            canonical_sub_state_code: 'confusion',
            should_intervene: 1,
            selected_strategy_id: 'ER-005',
            final_status: 'PUBLISHED'
          }]
        }
      }]
    }
  );

  assert.equal(coverage.scenarios[0].selectedPassed, true);
  assert.equal(coverage.scenarios[0].hasAllowedStrategy, true);
  assert.equal(coverage.passed, true);
});

test('cooldown suppression is counted separately from stale and superseded terminals', () => {
  const scenario = {
    scriptedDiscussion: {
      scenarios: [{
        id: 'RISK',
        scenarioType: 'required_intervention',
        expectedPrimaryState: 'frustration',
        canonicalSubState: 'frustration',
        shouldIntervene: true
      }],
      messages: []
    }
  };
  const statistics = buildCoverageStatistics(
    scenario,
    snapshot([], [], []),
    snapshot([
      { id: 1, final_status: 'SUPPRESSED', publish_status: 'SUPPRESSED', suppression_reason: 'COOLDOWN_ACTIVE' },
      { id: 2, final_status: 'STALE', publish_status: 'SKIPPED' },
      { id: 3, final_status: 'SUPERSEDED', publish_status: 'SKIPPED' }
    ], [], []),
    []
  );

  assert.equal(statistics.cooldown_suppressed, 1);
  assert.equal(statistics.stale_terminal, 1);
  assert.equal(statistics.superseded_terminal, 1);
  assert.equal(statistics.message_complete, true);
});
