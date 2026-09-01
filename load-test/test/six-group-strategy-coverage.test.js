const assert = require('node:assert/strict');
const test = require('node:test');

const scenario = require('../config/six-group-strategy-coverage');
const manifest = require('../../services/strategy_route_manifest.json');
const { RunState } = require('../src/runState');
const { assertDryRunStrategyCoverage } = require('../src/strategyCoverage');
const { applyCliOverrides } = require('../src/utils');

function scenarioMessages(id) {
  return scenario.scriptedDiscussion.messages.filter((message) => message.scenarioId === id);
}

test('six-group strategy suite has isolated four-person flows and all 28 strategy IDs', () => {
  assert.equal(scenario.totalStudents, 24);
  assert.equal(scenario.groupCount, 6);
  assert.equal(scenario.membersPerGroup, 4);
  assert.equal(scenario.stateSuite.scenarios.length, 30);
  assert.equal(scenario.scriptedDiscussion.messages.length, 263);
  assert.equal(scenario.strategyAudit.enabled, false);
  assert.equal(scenario.strategyAudit.requireCleanContext, false);
  assert.equal(scenario.strategyAudit.requireActualCoverage, false);
  assert.equal(scenario.stateSuite.enabled, false);
  assert.equal(scenario.strategyAudit.expectedEmotionAgentEnabled, false);
  assert.equal(scenario.rampUp.batchSize, 4);
  assert.equal(scenario.rampUp.intervalMs, 20 * 1000);
  assert.equal(scenario.rampUp.byGroup, true);
  assert.equal(scenario.flow.continueAfterScriptBoundaryFailure, true);
  assert.equal(scenario.flow.stopAfterScriptedMessagesComplete, false);
  assert.equal(scenario.discussionDurationMs, 40 * 60 * 1000);
  assert.equal(scenario.scriptedDiscussion.timeline.settleHeadroomSeconds, 4 * 60);
  assert.equal(scenario.scriptedDiscussion.timeline.lastMessageAtSeconds, 36 * 60);
  assert.ok(scenario.flow.actionTickMs >= 2000);
  assert.ok(scenario.flow.aiInputLockPollMs >= 1500);
  assert.doesNotThrow(() => assertDryRunStrategyCoverage(scenario));

  const strategyIds = new Set(scenario.scriptedDiscussion.expectedStrategyIds);
  assert.equal(strategyIds.size, 28);
  for (const prefix of ['EA', 'EE', 'ER', 'SS', 'OI']) {
    assert.ok([...strategyIds].some((id) => id.startsWith(`${prefix}-`)));
  }

  for (const groupCode of scenario.GROUP_CODES) {
    const messages = scenario.scriptedDiscussion.messages.filter(
      (message) => message.groupCode === groupCode
    );
    assert.ok(messages.length > 0);
    assert.deepEqual(new Set(messages.map((message) => message.memberNo)), new Set([1, 2, 3, 4]));
    assert.ok(messages.every((message) => message.groupCode === groupCode));
    const expectedStartSeconds = 5 + scenario.GROUP_CODES.indexOf(groupCode) * 20;
    assert.equal(Math.min(...messages.map((message) => message.atSeconds)), expectedStartSeconds);
    const ordered = [...messages].sort((a, b) => a.atSeconds - b.atSeconds);
    assert.ok(ordered[ordered.length - 1].atSeconds >= 35.5 * 60);
    assert.ok(ordered[ordered.length - 1].atSeconds <= 36 * 60);
    assert.ok(ordered[ordered.length - 1].atSeconds - ordered[0].atSeconds >= 34 * 60);
    for (let index = 1; index < ordered.length; index += 1) {
      const message = ordered[index];
      const previous = ordered[index - 1];
      assert.ok(message.atSeconds - previous.atSeconds >= 35, `${groupCode}: planned cadence`);
      if (message.scenarioId === previous.scenarioId) {
        assert.ok(message.minimumActualGapBeforeSeconds >= 25, `${message.scenarioId}: runtime cadence`);
      } else {
        assert.ok(message.minimumActualGapBeforeSeconds >= 75, `${message.scenarioId}: stage boundary`);
      }
    }
  }
});

test('student-visible text contains no state, strategy, or intervention test hints', () => {
  const forbidden = [
    /blocked_frustration/i,
    /positive_collaboration/i,
    /conflict_tension/i,
    /task_detached/i,
    /negative_silence/i,
    /(?:EA|EE|ER|SS|OI)-\d{3}/,
    /现在请介入/
  ];
  for (const message of scenario.scriptedDiscussion.messages) {
    for (const pattern of forbidden) {
      assert.equal(pattern.test(message.text), false, `${message.scenarioId}: ${message.text}`);
    }
  }
});

test('the requested 1,2,3,4,5,6 group list preserves a six-group run', () => {
  const selected = applyCliOverrides(scenario, { groups: '1,2,3,4,5,6' });
  assert.equal(selected.groupCount, 6);
});

test('real-time silence, thinking, marginalization, energy, and overload windows are preserved', () => {
  const g1d = scenarioMessages('G1-D');
  const g1dTrigger = g1d.find((message) => message.waitForExpectedInterventionAfter);
  assert.ok(g1dTrigger.minimumPauseSeconds >= 190);

  const g1e = scenarioMessages('G1-E');
  const g1ePause = g1e.find((message) => message.waitForNoInterventionAfter);
  assert.ok(g1ePause.noInterventionObservationSeconds >= 130);
  assert.ok(g1ePause.noInterventionObservationSeconds <= 150);
  assert.ok(g1ePause.minimumGapBeforeSeconds >= 120);
  assert.match(g1ePause.text, /独立核算和查资料/);

  const g3f = scenarioMessages('G3-F');
  assert.ok(g3f[7].atSeconds - g3f[2].atSeconds >= 130);

  const g4d = scenarioMessages('G4-D');
  assert.ok(g4d[6].atSeconds - g4d[0].atSeconds >= 300);

  const g6b = scenarioMessages('G6-B');
  assert.ok(g6b[g6b.length - 1].atSeconds - g6b[0].atSeconds >= 120);

  const g6c = scenarioMessages('G6-C');
  const g6cTrigger = g6c.find((message) => message.waitForExpectedInterventionAfter);
  assert.ok(g6cTrigger.atSeconds - g6c[0].atSeconds >= 300);
});

test('required intervention stages enforce 130 seconds since the previous observed publication', () => {
  for (const item of scenario.stateSuite.scenarios.filter(
    (entry) => entry.scenarioType === 'required_intervention'
  )) {
    assert.equal(item.messages[0].minimumPreviousInterventionGapSeconds, 130, item.id);
    assert.equal(
      item.messages.filter((message) => message.waitForExpectedInterventionAfter).length,
      1,
      item.id
    );
  }
});

test('optional support stages keep primary and overlay metadata without a publish gate', () => {
  const optional = scenario.stateSuite.scenarios.filter(
    (entry) => entry.scenarioType === 'optional_support'
  );
  assert.deepEqual(optional.map((entry) => entry.id), ['G2-E', 'G4-A', 'G6-B']);
  for (const item of optional) {
    assert.equal(item.expectedPrimaryState, item.canonicalSubState);
    assert.deepEqual(item.expectedOverlayStates, item.expectedOverlayTags);
    assert.equal(
      item.messages.some((message) => message.waitForExpectedInterventionAfter),
      false,
      item.id
    );
  }
});

test('Batch 7 calibrates focused scenarios to canonical state, route, and evidence metadata', () => {
  const focusedIds = ['G1-C', 'G3-F', 'G4-A', 'G4-B', 'G4-C', 'G4-D', 'G5-C', 'G5-D', 'G6-B', 'G6-C'];
  const expected = {
    'G1-C': ['confusion', [], 'required_intervention'],
    'G3-F': ['interpersonal_conflict', [], 'required_intervention'],
    'G4-A': ['standard', [], 'optional_support'],
    'G4-B': ['individual_marginalization', [], 'required_intervention'],
    'G4-C': ['individual_marginalization', [], 'required_intervention'],
    'G4-D': ['individual_marginalization', [], 'required_intervention'],
    'G5-C': ['perfunctory_detachment', [], 'required_intervention'],
    'G5-D': ['individual_marginalization', [], 'required_intervention'],
    'G6-B': ['standard', [], 'optional_support'],
    'G6-C': ['standard', ['high_intensity_overload'], 'required_intervention']
  };
  const byId = new Map(scenario.stateSuite.scenarios.map((item) => [item.id, item]));

  assert.equal(byId.size, 30);
  for (const item of byId.values()) {
    assert.equal(item.expected_primary_state, item.expectedPrimaryState, item.id);
    assert.deepEqual(item.expected_overlay_states, item.expectedOverlayStates, item.id);
    assert.equal(item.scenario_type, item.scenarioType, item.id);
    assert.equal(item.runtime_route_version, manifest.version, item.id);
    assert.equal(typeof item.expected_evidence_description, 'string', item.id);
    assert.ok(item.expected_evidence_description.trim(), item.id);
    assert.ok(item.candidate_start_sequence >= 1, item.id);
    assert.ok(item.candidate_end_sequence >= item.candidate_start_sequence, item.id);
    assert.ok(item.candidate_end_sequence <= item.messages.length, item.id);

    if (item.trigger_message_id) {
      const match = item.trigger_message_id.match(/:m(\d+)$/);
      assert.ok(match, `${item.id}: invalid planned trigger message key`);
      assert.equal(item.candidate_end_sequence, Number(match[1]), item.id);
      assert.ok(item.expected_evidence_message_ids.length > 0, item.id);
      for (const evidenceId of item.expected_evidence_message_ids) {
        const evidenceMatch = evidenceId.match(/:m(\d+)$/);
        assert.ok(evidenceMatch, `${item.id}: invalid evidence message key`);
        const sequence = Number(evidenceMatch[1]);
        assert.ok(
          sequence >= item.candidate_start_sequence && sequence <= item.candidate_end_sequence,
          `${item.id}: evidence must stay inside the candidate window`
        );
      }
    }
  }

  for (const id of focusedIds) {
    const item = byId.get(id);
    assert.ok(item, id);
    assert.deepEqual(
      [item.expectedPrimaryState, item.expectedOverlayStates, item.scenarioType],
      expected[id],
      id
    );
  }

  const g1c = byId.get('G1-C');
  assert.equal(g1c.messages[2].waitForExpectedInterventionAfter, true);
  assert.match(g1c.messages[2].text, /不会写/);

  const g3f = byId.get('G3-F');
  assert.equal(g3f.messages[2].waitForExpectedInterventionAfter, true);
  assert.match(g3f.messages[1].text, /太离谱/);

  const g4b = byId.get('G4-B');
  assert.ok(g4b.messages.filter((message) => message.memberNo === 3).length >= 2);
  assert.match(g4b.messages[1].text, /不展开/);
  assert.equal(g4b.messages[3].waitForExpectedInterventionAfter, true);

  const g4c = byId.get('G4-C');
  assert.ok(g4c.messages.filter((message) => message.memberNo === 4).length >= 3);
  assert.match(g4c.messages[5].text, /还没人回应/);

  const g4d = byId.get('G4-D');
  assert.ok(g4d.messages.filter((message) => message.memberNo === 2).length >= 4);
  assert.match(g4d.messages[6].text, /提醒两次/);

  const g5c = byId.get('G5-C');
  assert.ok(g5c.messages.some((message) => /随便|差不多|模板/.test(message.text)));

  const g5d = byId.get('G5-D');
  assert.ok(g5d.allowedStrategyIds.includes('EE-005'));
  assert.ok(g5d.allowedStrategyIds.includes('EA-003'));

  const g6c = byId.get('G6-C');
  assert.deepEqual(g6c.expectedOverlayStates, ['high_intensity_overload']);
  assert.equal(g6c.expectedPrimaryState, 'standard');
  assert.equal(g6c.messages.some((message) => /你这个|你不会|你总是/.test(message.text)), false);
  assert.match(g6c.messages.find((message) => message.waitForExpectedInterventionAfter).text, /没有争执或受挫/);
  assert.ok(scenario.timeouts.expectedPipelineDiscoveryMaxWaitMs >= 3 * 60 * 1000);
});

test('run state waits from the observed Agent publication time per group only', async () => {
  const runState = new RunState({
    runId: 'six-group-spacing',
    totalStudents: 8,
    minReadyStudents: 8,
    scriptedMessages: []
  });
  runState.recordObservedIntervention('G01', Date.now());
  const startedAt = Date.now();
  await runState.waitForPreviousInterventionGap('G01', 0.03);
  assert.ok(Date.now() - startedAt >= 20);

  const otherGroupStartedAt = Date.now();
  await runState.waitForPreviousInterventionGap('G02', 10);
  assert.ok(Date.now() - otherGroupStartedAt < 20);
});
