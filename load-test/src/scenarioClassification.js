const SCENARIO_TYPES = Object.freeze({
  REQUIRED_INTERVENTION: 'required_intervention',
  OPTIONAL_SUPPORT: 'optional_support',
  OBSERVATION_INHIBITION: 'observation_inhibition'
});

const REQUIRED_INTERVENTION_STATES = Object.freeze([
  'confusion',
  'frustration',
  'interpersonal_conflict',
  'burnout',
  'off_topic_unregulated',
  'perfunctory_detachment',
  'individual_marginalization'
]);

const OPTIONAL_SUPPORT_STATES = Object.freeze(['standard']);

const OBSERVATION_INHIBITION_STATES = Object.freeze([
  'deep_thinking',
  'execution_progress',
  'constructive_conflict',
  'off_topic_self_regulated',
  'unknown_sub_state'
]);

const REQUIRED_INTERVENTION_STATE_SET = new Set(REQUIRED_INTERVENTION_STATES);
const OPTIONAL_SUPPORT_STATE_SET = new Set(OPTIONAL_SUPPORT_STATES);
const OBSERVATION_INHIBITION_STATE_SET = new Set(OBSERVATION_INHIBITION_STATES);

function unique(items) {
  return [...new Set((items || []).filter(Boolean).map((item) => String(item)))];
}

function firstValue(item, ...keys) {
  for (const key of keys) {
    if (item && item[key] !== undefined && item[key] !== null) return item[key];
  }
  return null;
}

function expectedPrimaryState(item) {
  return firstValue(
    item,
    'expectedPrimaryState',
    'expected_primary_state',
    'canonicalSubState',
    'canonical_sub_state',
    'expectedProcessState',
    'expected_process_state'
  );
}

function expectedOverlayStates(item) {
  return unique(firstValue(
    item,
    'expectedOverlayStates',
    'expected_overlay_states',
    'expectedOverlayTags',
    'expected_overlay_tags'
  ));
}

function explicitScenarioType(item) {
  const value = firstValue(item, 'scenarioType', 'scenario_type', 'acceptanceType', 'acceptance_type');
  return Object.values(SCENARIO_TYPES).includes(value) ? value : null;
}

function scenarioTypeFor(item) {
  const explicit = explicitScenarioType(item);
  if (explicit) return explicit;

  const primaryState = expectedPrimaryState(item);
  const overlays = expectedOverlayStates(item);
  const inhibitionStrategyId = firstValue(
    item,
    'inhibitionStrategyId',
    'inhibition_strategy_id'
  );

  if (overlays.includes('stage_achievement') || (
    OPTIONAL_SUPPORT_STATE_SET.has(primaryState) &&
    !overlays.includes('high_intensity_overload')
  )) {
    return SCENARIO_TYPES.OPTIONAL_SUPPORT;
  }
  if (inhibitionStrategyId || OBSERVATION_INHIBITION_STATE_SET.has(primaryState)) {
    return SCENARIO_TYPES.OBSERVATION_INHIBITION;
  }
  if (REQUIRED_INTERVENTION_STATE_SET.has(primaryState)) {
    return SCENARIO_TYPES.REQUIRED_INTERVENTION;
  }
  return item && item.shouldIntervene === true
    ? SCENARIO_TYPES.REQUIRED_INTERVENTION
    : SCENARIO_TYPES.OBSERVATION_INHIBITION;
}

function classifyScenario(item) {
  const primaryState = expectedPrimaryState(item);
  const overlayStates = expectedOverlayStates(item);
  return {
    type: scenarioTypeFor(item),
    primaryState: primaryState ? String(primaryState) : null,
    overlayStates,
    requiredIntervention: scenarioTypeFor(item) === SCENARIO_TYPES.REQUIRED_INTERVENTION,
    optionalSupport: scenarioTypeFor(item) === SCENARIO_TYPES.OPTIONAL_SUPPORT,
    observationInhibition: scenarioTypeFor(item) === SCENARIO_TYPES.OBSERVATION_INHIBITION
  };
}

function scenariosByType(scenarios, type) {
  return (scenarios || []).filter((item) => scenarioTypeFor(item) === type);
}

module.exports = {
  OBSERVATION_INHIBITION_STATES,
  OPTIONAL_SUPPORT_STATES,
  REQUIRED_INTERVENTION_STATES,
  SCENARIO_TYPES,
  classifyScenario,
  expectedOverlayStates,
  expectedPrimaryState,
  scenarioTypeFor,
  scenariosByType
};
