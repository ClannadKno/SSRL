const assert = require('node:assert/strict');
const test = require('node:test');

const { buildAiLockProbePayload, waitForExpectedIntervention } = require('../src/actions');
const { Metrics } = require('../src/metrics');
const {
  pipelineReplacementReport,
  pipelineTerminal,
  pipelineTiming
} = require('../src/expectedGate');

function scenario(overrides = {}) {
  return {
    selectors: {
      discussionInput: '#messageInput',
      sendButton: '.send-btn',
      helpButton: '.help-btn',
      aiLockHint: '#aiLockHint'
    },
    timeouts: {
      apiResponseMs: 1000,
      expectedPipelineDiscoveryMaxWaitMs: 60,
      aiInputLockObserverGraceMs: 100
    },
    flow: {
      expectedInterventionMaxWaitMs: 500,
      expectedPipelinePollMs: 1,
      aiInputLockPollMs: 1,
      messageInputPollMs: 1,
      verifyAiInputLockApiReject: false
    },
    ...overrides
  };
}

function pageForBodies(bodies, uiStates = []) {
  let bodyIndex = 0;
  const uiQueue = [...uiStates];
  const fixedDetails = {
    inputReadOnly: true,
    inputDisabled: false,
    inputAriaDisabled: 'true',
    sendDisabled: true,
    helpDisabled: true,
    hintVisible: true
  };
  return {
    evaluate: async (_fn, argument) => {
      if (argument && typeof argument === 'object' && argument.url) {
        const body = bodies[Math.min(bodyIndex, bodies.length - 1)];
        bodyIndex += 1;
        return { status: 200, data: body };
      }
      if (typeof argument === 'string') {
        return uiQueue.shift() || {
          available: true,
          locked: false,
          readOnly: false,
          disabled: false,
          ariaDisabled: 'false',
          className: ''
        };
      }
      if (argument && typeof argument === 'object' && argument.input) return fixedDetails;
      return null;
    },
    waitForFunction: async () => true
  };
}

function pipeline(status, overrides = {}) {
  return {
    pipeline_run_id: overrides.pipeline_run_id || `pipeline-${status}`,
    trigger_message_id: 101,
    trigger_message_at: '2026-08-01T00:00:00.000Z',
    created_at: '2026-08-01T00:00:00.000Z',
    updated_at: '2026-08-01T00:00:01.500Z',
    room_lock_acquired_at: '2026-08-01T00:00:00.250Z',
    room_lock_released_at: '2026-08-01T00:00:00.750Z',
    final_status: status,
    publish_status: status === 'PUBLISHED' ? 'PUBLISHED' : 'NOT_PUBLISHED',
    published_message_id: status === 'PUBLISHED' ? 202 : null,
    ...overrides
  };
}

function observer(metrics) {
  return { metrics, studentId: 'S1-G01-M1', groupCode: 'G01' };
}

test('expected gate ends on every pipeline terminal without requiring an Agent message', async () => {
  const cases = [
    ['PUBLISHED', 'PUBLISHED'],
    ['SUPPRESSED_COOLDOWN', 'SUPPRESSED'],
    ['STALE', 'STALE'],
    ['SUPERSEDED', 'SUPERSEDED'],
    ['FAILED', 'FAILED'],
    ['SKIPPED', 'SKIPPED']
  ];

  for (const [status, expectedReason] of cases) {
    const metrics = new Metrics({
      runId: `terminal-${status}`,
      scenario: scenario()
    });
    const result = await waitForExpectedIntervention(
      pageForBodies([{ ai_lock: { locked: false }, room: { state: 'DISCUSSING' }, pipelines: [pipeline(status)] }]),
      scenario(),
      observer(metrics),
      { afterMessageId: 101, scenarioId: `CASE-${status}` }
    );

    assert.equal(result.terminalReason, expectedReason);
    assert.equal(metrics.counters.expectedInterventionCompleted, 1);
    assert.equal(metrics.counters.expectedInterventionFailed, 0);
    const completed = metrics.events.find((event) => event.type === 'expected_intervention_completed');
    assert.equal(completed.terminal_reason, expectedReason);
    assert.equal(completed.pipeline_run_id, `pipeline-${status}`);
    assert.equal(completed.pipeline_wait_duration, 1500);
    assert.equal(completed.lease_duration, 500);
    assert.equal(completed.published_message_id, expectedReason === 'PUBLISHED' ? 202 : null);
  }
});

test('expected gate ends for pipeline A while a consecutive lease B is active', async () => {
  const metrics = new Metrics({ runId: 'consecutive-lease', scenario: scenario() });
  const result = await waitForExpectedIntervention(
    pageForBodies([
      {
        ai_lock: { locked: true, active_intervention_run_id: 'A', lock_owner_run_id: 'A' },
        room: { state: 'AI_INTERVENING', active_intervention_run_id: 'A' },
        pipelines: [pipeline('PENDING', { pipeline_run_id: 'A', final_status: 'PENDING', publish_status: 'NOT_READY' })]
      },
      {
        ai_lock: { locked: true, active_intervention_run_id: 'B', lock_owner_run_id: 'B' },
        room: { state: 'AI_INTERVENING', active_intervention_run_id: 'B' },
        pipelines: [pipeline('PUBLISHED', { pipeline_run_id: 'A' })]
      }
    ]),
    scenario(),
    observer(metrics),
    { afterMessageId: 101, scenarioId: 'CONSECUTIVE' }
  );

  assert.equal(result.pipelineRunId, 'A');
  assert.equal(result.terminalReason, 'PUBLISHED');
  const observed = metrics.events.filter((event) => event.type === 'ai_input_lock_observed');
  assert.equal(observed.length, 2);
  assert.equal(observed[0].sequential_lease_count, 1);
  assert.equal(observed[1].sequential_lease_count, 2);
  assert.equal(observed[0].lock_owner_pipeline_run_id, 'A');
  assert.equal(observed[1].lock_owner_pipeline_run_id, 'B');
});

test('server release with delayed UI restoration is recorded as LOCK_OBSERVER_RACE', async () => {
  const metrics = new Metrics({ runId: 'observer-race', scenario: scenario() });
  const result = await waitForExpectedIntervention(
    pageForBodies([
      {
        ai_lock: { locked: true, active_intervention_run_id: 'A', lock_owner_run_id: 'A' },
        room: { state: 'AI_INTERVENING', active_intervention_run_id: 'A' },
        pipelines: [pipeline('PENDING', { pipeline_run_id: 'A', final_status: 'PENDING', publish_status: 'NOT_READY' })]
      },
      {
        ai_lock: { locked: false },
        room: { state: 'DISCUSSING' },
        pipelines: [pipeline('SUPPRESSED_COOLDOWN', { pipeline_run_id: 'A' })]
      }
    ], [
      { available: true, locked: true, readOnly: true, disabled: false, ariaDisabled: 'true', className: 'ai-locked' },
      { available: true, locked: false, readOnly: false, disabled: false, ariaDisabled: 'false', className: '' }
    ]),
    scenario(),
    observer(metrics),
    { afterMessageId: 101, scenarioId: 'RACE' }
  );

  assert.equal(result.terminalReason, 'SUPPRESSED');
  assert.equal(metrics.counters.lockObserverRaces, 1);
  assert.equal(metrics.counters.clientInputLockRestored, 1);
  assert.equal(metrics.events.find((event) => event.type === 'lock_observer_race').reason, 'LOCK_OBSERVER_RACE');
  assert.ok(metrics.events.find((event) => event.type === 'client_input_restored').continuous_block_duration >= 0);
});

test('expected gate fails quickly and explicitly when the trigger has no pipeline', async () => {
  const metrics = new Metrics({ runId: 'missing-pipeline', scenario: scenario() });
  await assert.rejects(
    waitForExpectedIntervention(
      pageForBodies([{ ai_lock: { locked: false }, room: { state: 'DISCUSSING' }, pipelines: [], messages: [] }]),
      scenario(),
      observer(metrics),
      { afterMessageId: 101, scenarioId: 'NO_PIPELINE' }
    ),
    (error) => error.expectedGateReason === 'pipeline_not_found'
  );
  const failed = metrics.events.find((event) => event.type === 'expected_intervention_failed');
  assert.equal(failed.reason, 'pipeline_not_found');
  assert.equal(failed.pipeline_found, false);
});

test('AI lock API probe cannot become a student message after a lease race', () => {
  const payload = buildAiLockProbePayload(17);
  assert.equal(payload.group_id, 17);
  assert.equal(payload.content, '');
  assert.match(payload.client_message_id, /^ai-lock-probe-/);
});

test('pipeline timing keeps processing and lease durations separate', () => {
  const value = pipelineTiming(pipeline('PUBLISHED'));
  assert.equal(value.pipeline_wait_duration, 1500);
  assert.equal(value.lease_duration, 500);
  assert.notEqual(value.pipeline_wait_duration, value.lease_duration);
  assert.deepEqual(pipelineTerminal(pipeline('SUPPRESSED_COOLDOWN')), {
    terminalReason: 'SUPPRESSED',
    terminalStatus: 'SUPPRESSED_COOLDOWN',
    isTerminal: true
  });
});

test('replacement report keeps stale trigger state separate from latest replacement state', () => {
  const original = pipeline('STALE', {
    pipeline_run_id: 'original',
    replaced_by_pipeline_run_id: 'replacement',
    trigger_level_state: 'interpersonal_conflict',
    latest_state: 'execution_progress',
    latest_should_intervene: 0
  });
  const replacement = pipeline('SUPPRESSED', {
    pipeline_run_id: 'replacement',
    stage2_status: 'SUCCEEDED',
    canonical_sub_state_code: 'execution_progress',
    should_intervene: 0,
    publish_status: 'SKIPPED',
    parent_run_id: 'original'
  });

  assert.deepEqual(
    pipelineReplacementReport(original, [original, replacement]),
    {
      original_pipeline_run_id: 'original',
      original_pipeline_terminal: 'STALE',
      replacement_pipeline_run_id: 'replacement',
      replacement_final_state: 'execution_progress',
      replacement_should_intervene: 0,
      replacement_publish_status: 'SKIPPED',
      trigger_level_state: 'interpersonal_conflict',
      latest_state: 'execution_progress',
      latest_should_intervene: 0
    }
  );
});
