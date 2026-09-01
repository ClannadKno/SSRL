const FINAL_STATE_CODES = [
  'positive_collaboration',
  'negative_silence',
  'conflict_tension',
  'blocked_frustration',
  'task_detached',
  'unknown'
];

const FINAL_STATE_SET = new Set(FINAL_STATE_CODES);
const PRIMARY_SUB_STATE_CODES = [
  'standard',
  'deep_thinking',
  'execution_progress',
  'constructive_conflict',
  'interpersonal_conflict',
  'confusion',
  'frustration',
  'burnout',
  'off_topic_self_regulated',
  'off_topic_unregulated',
  'perfunctory_detachment',
  'individual_marginalization'
];
const PRIMARY_SUB_STATE_SET = new Set(PRIMARY_SUB_STATE_CODES);
const PROCESS_STATE_CODES = ['unknown_sub_state'];
const STATE_SUITE_CODE_SET = new Set([...PRIMARY_SUB_STATE_CODES, ...PROCESS_STATE_CODES]);

function unique(items) {
  return [...new Set((items || []).filter(Boolean).map((item) => String(item)))];
}

function stateFromExpectedItem(item) {
  return typeof item === 'string' ? item : item && item.state;
}

function expectedStatesForScenario(scenario) {
  const script = scenario && scenario.scriptedDiscussion;
  if (!script) return [];
  if (Array.isArray(script.expectedFinalStates) && script.expectedFinalStates.length) {
    return unique(script.expectedFinalStates);
  }
  if (Array.isArray(script.expectedRuleStates) && script.expectedRuleStates.length) {
    return unique(script.expectedRuleStates.map(stateFromExpectedItem));
  }
  return plannedStatesForScenario(scenario);
}

function plannedStatesForScenario(scenario) {
  const messages = scenario && scenario.scriptedDiscussion && scenario.scriptedDiscussion.messages;
  if (!Array.isArray(messages)) return [];
  return unique(messages.map((message) => message.state));
}

function countScriptedStates(events, eventType) {
  const counts = {};
  for (const event of events || []) {
    if (event.type !== eventType) continue;
    const match = String(event.kind || '').match(/^script:(.+)$/);
    if (!match) continue;
    const state = match[1];
    counts[state] = (counts[state] || 0) + 1;
  }
  return counts;
}

function statesFromCounts(counts) {
  return Object.keys(counts || {}).filter((state) => counts[state] > 0).sort();
}

function missingStates(expected, observed) {
  const observedSet = new Set(observed || []);
  return (expected || []).filter((state) => !observedSet.has(state));
}

function buildScriptedStateCoverage(scenario, events) {
  const script = scenario && scenario.scriptedDiscussion;
  if (!script || !Array.isArray(script.messages) || !script.messages.length) {
    return null;
  }

  const expected = expectedStatesForScenario(scenario);
  const planned = plannedStatesForScenario(scenario);
  const attemptedCounts = countScriptedStates(events, 'message_attempt');
  const successfulCounts = countScriptedStates(events, 'message_success');
  const attempted = statesFromCounts(attemptedCounts);
  const successful = statesFromCounts(successfulCounts);
  const expectedForComparison = expected.length ? expected : planned;
  const missingPlanned = missingStates(expectedForComparison, planned);
  const missingAttempted = missingStates(expectedForComparison, attempted);
  const missingSuccessful = missingStates(expectedForComparison, successful);
  const canonicalSuite = Boolean(scenario && scenario.stateSuite && scenario.stateSuite.enabled);
  const expectedStateSet = canonicalSuite ? STATE_SUITE_CODE_SET : FINAL_STATE_SET;
  const nonFinalPlannedStates = planned.filter(
    (state) => !expectedStateSet.has(state) && state !== 'recovery_bridge'
  );
  const nonFinalExpectedStates = expected.filter((state) => !expectedStateSet.has(state));
  const requiredAllFinalStates = Boolean(script.mustCoverAllFinalStates);

  return {
    coverageSource: 'script_markers_only',
    provesServerDetection: false,
    expected: expectedForComparison,
    planned,
    attempted,
    successful,
    attemptedCounts,
    successfulCounts,
    missingPlanned,
    missingAttempted,
    missingSuccessful,
    nonFinalExpectedStates,
    nonFinalPlannedStates,
    requiredAllFinalStates,
    allFinalStatesExpected: missingStates(FINAL_STATE_CODES, expectedForComparison).length === 0,
    allFinalStatesPlanned: missingStates(FINAL_STATE_CODES, planned).length === 0,
    allFinalStatesSuccessful: missingStates(FINAL_STATE_CODES, successful).length === 0,
    passed: missingPlanned.length === 0 && missingSuccessful.length === 0
  };
}

function assertDryRunStateCoverage(scenario) {
  const script = scenario && scenario.scriptedDiscussion;
  if (!script || !Array.isArray(script.messages) || !script.messages.length) return;

  const expected = expectedStatesForScenario(scenario);
  const planned = plannedStatesForScenario(scenario);
  const expectedForComparison = expected.length ? expected : planned;
  const missingPlanned = missingStates(expectedForComparison, planned);
  if (missingPlanned.length) {
    throw new Error(`Scripted discussion is missing planned states: ${missingPlanned.join(', ')}`);
  }

  if (!script.mustCoverAllFinalStates) return;

  const missingExpectedFinalStates = missingStates(FINAL_STATE_CODES, expectedForComparison);
  if (missingExpectedFinalStates.length) {
    throw new Error(`Scripted discussion expectedFinalStates does not cover final states: ${missingExpectedFinalStates.join(', ')}`);
  }

  const missingPlannedFinalStates = missingStates(FINAL_STATE_CODES, planned);
  if (missingPlannedFinalStates.length) {
    throw new Error(`Scripted discussion messages do not cover final states: ${missingPlannedFinalStates.join(', ')}`);
  }

  const nonFinalExpectedStates = expectedForComparison.filter((state) => !FINAL_STATE_SET.has(state));
  if (nonFinalExpectedStates.length) {
    throw new Error(`Scripted discussion expected states must use final state codes only: ${nonFinalExpectedStates.join(', ')}`);
  }

  const nonFinalPlannedStates = planned.filter((state) => !FINAL_STATE_SET.has(state));
  if (nonFinalPlannedStates.length) {
    throw new Error(`Scripted discussion message states must use final state codes only: ${nonFinalPlannedStates.join(', ')}`);
  }
}

module.exports = {
  FINAL_STATE_CODES,
  PROCESS_STATE_CODES,
  PRIMARY_SUB_STATE_CODES,
  assertDryRunStateCoverage,
  buildScriptedStateCoverage,
  expectedStatesForScenario,
  plannedStatesForScenario
};
