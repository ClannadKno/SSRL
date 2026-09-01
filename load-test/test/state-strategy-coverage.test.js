const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const packageJson = require('../package.json');
const suiteScenario = require('../config/one-group-full-flow-trigger-states');
const {
  buildActualStateCoverage,
  buildActualServerCoverage,
  buildCanonicalDbCoverage,
  buildMessageSendCoverage
} = require('../src/serverCoverage');
const {
  assertAgentFlagCompatibility,
  buildEmptyStateSuiteBaseline,
  buildExportAuditFromEntries,
  parseCsv,
  readZipEntries,
  sanitizeAgentAudit
} = require('../src/strategyAudit');
const {
  buildScriptedStateCoverage,
  PRIMARY_SUB_STATE_CODES,
  PROCESS_STATE_CODES
} = require('../src/stateCoverage');
const {
  assertDryRunStrategyCoverage,
  buildActualStrategyCoverage,
  expectedStrategyIdsForScenario
} = require('../src/strategyCoverage');
const { Metrics } = require('../src/metrics');
const {
  buildP0Batch6Acceptance,
  writeP0Batch6Bundle
} = require('../src/p0Batch6Audit');
const { applyCliOverrides, parseArgs } = require('../src/utils');

function auditSnapshot(stateScenario, phase, mutateRun = (run) => run, options = {}) {
  const run = mutateRun({
    id: 100,
    canonical_sub_state_code: stateScenario.canonicalSubState,
    final_sub_state_code: stateScenario.canonicalSubState,
    secondary_sub_state_tags: stateScenario.expectedOverlayTags || [],
    should_intervene: stateScenario.shouldIntervene ? '是' : '否',
    inhibition_strategy_id: stateScenario.inhibitionStrategyId || null,
    selected_strategy_id: stateScenario.selectedStrategyId || stateScenario.allowedStrategyIds[0],
    supporting_strategy_ids: [],
    strategy_candidate_ids: options.strategyIds || [stateScenario.allowedStrategyIds[0]],
    final_status: stateScenario.shouldIntervene ? 'PUBLISHED' : 'SUPPRESSED',
    publish_status: stateScenario.shouldIntervene ? 'PUBLISHED' : 'NOT_PUBLISHED',
    detected_self_regulation: stateScenario.detectedSelfRegulation === undefined
      ? '历史数据未记录'
      : (stateScenario.detectedSelfRegulation ? '是' : '否')
  }, stateScenario);
  return {
    phase,
    sessionId: 42,
    agentFlags: {
      detectionEnabled: true,
      strategyAgentEnabled: true,
      emotionAgentEnabled: Boolean(options.emotionAgentEnabled)
    },
    groups: [{
      scenarioId: stateScenario.id,
      groupCode: 'G01',
      groupId: 1,
      audit: {
        message_timeline: [],
        assessment_batches: options.assessmentBatches || [],
        strategy_pipeline_runs: phase === 'baseline' ? [] : [run]
      }
    }]
  };
}

function scenarioEvents(stateScenario, firstMessageId = 501) {
  return (stateScenario.messages || []).map((message, index) => ({
    type: 'message_success',
    studentId: `S1-G01-M${message.memberNo}`,
    seq: index + 1,
    scenarioId: stateScenario.id,
    kind: `script:${stateScenario.canonicalSubState}`,
    messageId: firstMessageId + index
  }));
}

function strictAuditSnapshot(
  stateScenario,
  phase,
  mutateRun = (run) => run,
  options = {}
) {
  const base = auditSnapshot(stateScenario, phase, mutateRun, options);
  const group = base.groups[0];
  const isBaseline = phase === 'baseline';
  const events = scenarioEvents(stateScenario);
  const messageRows = isBaseline ? [] : events.map((event, index) => ({
    id: event.messageId,
    message_id: event.messageId,
    sequence: index + 1,
    group_id: 1,
    session_id: 42,
    discussion_id: 77,
    task_id: 9,
    role: 'student',
    sender_type: 'student'
  }));
  const failure = Boolean(stateScenario.expectedFailureFallback);
  const processState = stateScenario.expectedProcessState === 'unknown_sub_state';
  const rawRun = (group.audit.strategy_pipeline_runs || [])[0] || {};
  const run = isBaseline ? null : {
    ...rawRun,
    group_id: 1,
    session_id: 42,
    discussion_id: 77,
    task_id: 9,
    canonical_sub_state_code: failure ? null : rawRun.canonical_sub_state_code,
    final_sub_state_code: (failure || processState) ? null : rawRun.final_sub_state_code,
    evidence_message_ids: failure ? [] : [events[0].messageId],
    evidence_sequences: failure ? [] : [1],
    strategy_candidate_ids: rawRun.strategy_candidate_ids || [],
    supporting_strategy_ids: rawRun.supporting_strategy_ids || []
  };
  group.audit.strategy_pipeline_runs = run ? [run] : [];
  const assessmentBatches = isBaseline ? [] : (options.assessmentBatches || []);
  group.audit.assessment_batches = assessmentBatches;
  const segment = isBaseline ? null : {
    id: 301,
    group_id: 1,
    session_id: 42,
    discussion_id: 77,
    task_id: 9,
    canonical_sub_state_code: failure ? null : stateScenario.canonicalSubState,
    assessment_status: (failure || processState) ? 'unclassified' : 'confirmed',
    assignment_source: failure
      ? 'batch_unclassified'
      : (processState ? 'unknown_sub_state' : 'model_segment'),
    start_message_id: events[0].messageId,
    end_message_id: events.at(-1).messageId,
    start_sequence: 1,
    end_sequence: events.length,
    evidence_message_ids: events.map((event) => event.messageId),
    source: failure ? 'session_finalizer' : 'strategy_llm',
    dedupe_key: `test-${stateScenario.id}`
  };
  const intervention = !isBaseline && stateScenario.shouldIntervene ? {
    id: 401,
    group_id: 1,
    session_id: 42,
    discussion_id: 77,
    task_id: 9,
    strategy_pipeline_run_id: run.id,
    canonical_sub_state_code: stateScenario.canonicalSubState,
    selected_strategy_id: run.selected_strategy_id,
    status: 'PUBLISHED',
    publish_status: 'PUBLISHED',
    agent_type: 'strategy'
  } : null;
  group.dbAudit = {
    schema_version: 'state-suite-audit/2',
    audit_available: true,
    available_tables: {
      messages: true,
      collaboration_state_segments: true,
      state_assessment_batches: true,
      strategy_pipeline_runs: true,
      intervention_runs: true,
      emotion_reflection_slots: true,
      strategy_pipeline_latency_events: true
    },
    scope: {
      group_id: 1,
      session_id: 42,
      discussion_id: 77,
      task_id: 9
    },
    tables: {
      messages: messageRows,
      collaboration_state_segments: segment ? [segment] : [],
      state_assessment_batches: assessmentBatches.map((batch) => ({
        ...batch,
        group_id: 1,
        session_id: 42,
        discussion_id: 77,
        task_id: 9
      })),
      strategy_pipeline_runs: run ? [run] : [],
      intervention_runs: intervention ? [intervention] : [],
      emotion_reflection_slots: [],
      strategy_pipeline_latency_events: []
    }
  };
  const assignments = messageRows.map((message) => ({
    ...message,
    final_sub_state_code: (failure || processState) ? null : stateScenario.canonicalSubState,
    assessment_status: (failure || processState) ? 'unclassified' : 'confirmed',
    assignment_source: failure
      ? 'batch_unclassified'
      : (processState ? 'unknown_sub_state' : 'model_segment'),
    error_code: failure ? 'truncated_response' : null,
    state_overlays: stateScenario.expectedOverlayTags || []
  }));
  const stateSegments = segment && !failure && !processState ? [{
    ...segment,
    final_sub_state_code: stateScenario.canonicalSubState,
    state_overlays: stateScenario.expectedOverlayTags || []
  }] : [];
  const currentState = {
    final_sub_state_code: (failure || processState) ? null : stateScenario.canonicalSubState,
    assessment_status: (failure || processState) ? 'unclassified' : 'confirmed',
    assignment_source: failure
      ? 'batch_unclassified'
      : (processState ? 'unknown_sub_state' : 'model_segment')
  };
  group.teacherApis = {
    emotionTrend: {
      resolved_session_id: 42,
      message_state_context: { discussion_id: 77 },
      current_state: currentState,
      state_segments: stateSegments
    },
    emotionReview: {
      resolved_session_id: 42,
      message_state_context: { discussion_id: 77 },
      current_state: currentState,
      messages: assignments,
      state_segments: stateSegments
    },
    agentAudit: group.audit,
    teacherGroups: {
      groups: [{
        group_id: 1,
        group_code: 'G01',
        session_id: 42,
        final_sub_state_code: currentState.final_sub_state_code,
        assessment_status: currentState.assessment_status
      }]
    },
    groupDetail: {
      session_id: 42,
      current_state: currentState,
      canonical_segments: stateSegments
    }
  };
  const strategyExport = run ? [{
    pipeline_run_id: run.id,
    group_id: '1',
    session_id: '42',
    discussion_id: '77',
    task_id: '9',
    canonical_sub_state_code: run.canonical_sub_state_code || '',
    state_overlays: JSON.stringify(stateScenario.expectedOverlayTags || []),
    coarse_state_code: 'positive_collaboration',
    evidence_message_ids: JSON.stringify(run.evidence_message_ids || []),
    should_intervene: run.should_intervene,
    inhibition_strategy_id: run.inhibition_strategy_id || '',
    candidate_strategy_ids: JSON.stringify(run.strategy_candidate_ids || []),
    selected_strategy_id: run.selected_strategy_id || '',
    supporting_strategy_ids: JSON.stringify(run.supporting_strategy_ids || []),
    publish_status: run.publish_status || '',
    final_status: run.final_status || ''
  }] : [];
  group.exportAudit = {
    available: true,
    missingFiles: [],
    files: {
      'messages.csv': assignments.map((message) => ({
        ...message,
        message_id: message.id,
        group_id: '1',
        session_id: '42',
        discussion_id: '77',
        task_id: '9',
        coarse_state_code: 'positive_collaboration'
      })),
      'strategy_pipeline_runs.csv': strategyExport,
      'interventions.csv': intervention ? [{
        intervention_run_id: intervention.id,
        group_id: '1',
        session_id: '42',
        discussion_id: '77',
        task_id: '9',
        strategy_pipeline_run_id: intervention.strategy_pipeline_run_id,
        canonical_sub_state_code: intervention.canonical_sub_state_code,
        selected_strategy_id: intervention.selected_strategy_id,
        status: 'PUBLISHED',
        publish_status: 'PUBLISHED'
      }] : [],
      'unified-events.csv': assignments.map((message) => ({
        event_type: 'state_assignment',
        event_id: message.id,
        related_id: message.id,
        actor_role: 'student',
        group_id: '1',
        session_id: '42',
        discussion_id: '77',
        task_id: '9',
        final_sub_state_code: message.final_sub_state_code || '',
        coarse_state_code: 'positive_collaboration',
        assessment_status: message.assessment_status,
        assignment_source: message.assignment_source,
        error_code: message.error_code || ''
      }))
    }
  };
  base.auditAvailable = true;
  base.validationMode = 'real_coverage';
  return base;
}

function storedZip(entries) {
  const localParts = [];
  const centralParts = [];
  let offset = 0;
  for (const [name, raw] of Object.entries(entries)) {
    const nameBuffer = Buffer.from(name);
    const data = Buffer.from(raw);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0x800, 6);
    local.writeUInt16LE(0, 8);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBuffer.length, 26);
    localParts.push(local, nameBuffer, data);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(0x800, 8);
    central.writeUInt16LE(0, 10);
    central.writeUInt32LE(data.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(nameBuffer.length, 28);
    central.writeUInt32LE(offset, 42);
    centralParts.push(central, nameBuffer);
    offset += local.length + nameBuffer.length + data.length;
  }
  const centralDirectory = Buffer.concat(centralParts);
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0);
  eocd.writeUInt16LE(Object.keys(entries).length, 8);
  eocd.writeUInt16LE(Object.keys(entries).length, 10);
  eocd.writeUInt32LE(centralDirectory.length, 12);
  eocd.writeUInt32LE(offset, 16);
  return Buffer.concat([...localParts, centralDirectory, eocd]);
}

function stateCaseName(item) {
  return (item.expectedOverlayTags || [])[0] ||
    item.expectedProcessState ||
    item.canonicalSubState;
}

test('default S01-S16 plan is a four-person 12+3+1 teacher-controlled discussion', () => {
  assert.doesNotThrow(() => assertDryRunStrategyCoverage(suiteScenario));
  assert.equal(suiteScenario.totalStudents, 4);
  assert.equal(suiteScenario.groupCount, 1);
  assert.equal(suiteScenario.membersPerGroup, 4);
  assert.equal(suiteScenario.scriptedDiscussion.scenarios.length, 16);
  assert.deepEqual(
    new Set(suiteScenario.scriptedDiscussion.scenarios
      .filter((item) => !item.expectedOverlayTags && !item.expectedProcessState)
      .map((item) => item.canonicalSubState)),
    new Set(PRIMARY_SUB_STATE_CODES)
  );
  assert.deepEqual(
    suiteScenario.scriptedDiscussion.scenarios.flatMap((item) => item.expectedOverlayTags || []),
    ['psychological_safety_risk', 'high_intensity_overload', 'stage_achievement']
  );
  assert.deepEqual(
    suiteScenario.scriptedDiscussion.scenarios
      .filter((item) => item.expectedProcessState)
      .map((item) => item.expectedProcessState),
    PROCESS_STATE_CODES
  );
  assert.deepEqual(new Set(suiteScenario.scriptedDiscussion.scenarios.map((item) => item.groupCode)), new Set(['G01']));
  assert.equal(suiteScenario.stateSuite.enabled, false);
  assert.equal(suiteScenario.stateSuite.mode, 'discussion-only');
  assert.equal(suiteScenario.stateSuite.isolation, 'teacher-controlled-current-session');
  assert.equal(suiteScenario.stateSuite.includeRecoveryBridges, false);
  assert.equal(suiteScenario.strategyAudit.enabled, false);
  assert.equal(suiteScenario.strategyAudit.requireActualCoverage, false);
  assert.equal(suiteScenario.flow.enforceScriptedInterventionExpectations, false);
  assert.equal(suiteScenario.flow.verifyAiInputLock, true);
  assert.equal(suiteScenario.flow.verifyAiInputLockApiReject, false);
  assert.equal(suiteScenario.flow.submitPreQuestionnaires, true);
  assert.equal(suiteScenario.flow.submitPostQuestionnaires, false);
  assert.equal(suiteScenario.scriptedDiscussion.requireIsolatedDiscussions, false);
  assert.equal(suiteScenario.scriptedDiscussion.mustCoverAllExpectedStrategies, false);
  assert.ok(expectedStrategyIdsForScenario(suiteScenario).length > 12);
});

test('the one-group command uses the student runner and never the session lifecycle runner', () => {
  assert.equal(
    packageJson.scripts['test:one-group:states'],
    'node src/run.js --scenario one-group-full-flow-trigger-states'
  );
  assert.equal(
    packageJson.scripts['dry-run:one-group:states'],
    'node src/run.js --scenario one-group-full-flow-trigger-states --dry-run'
  );
});

test('real coverage audit allows the final scheduler and model batch to settle', () => {
  const strictSuite = suiteScenario.forSuite('primary-substates');
  assert.equal(strictSuite.stateSuite.enabled, true);
  assert.equal(strictSuite.strategyAudit.enabled, true);
  assert.equal(strictSuite.flow.enforceScriptedInterventionExpectations, true);
  assert.equal(strictSuite.flow.verifyAiInputLockApiReject, true);
  assert.ok(
    strictSuite.strategyAudit.maxWaitMs >= 3 * 60 * 1000,
    'state-suite audit must cover the trailing scheduler/model completion window'
  );
});

test('p0 batch6 suite uses four students, both Agents, and a real five-minute slot', () => {
  const round = suiteScenario.forSuite('p0-batch6');
  assert.equal(round.totalStudents, 4);
  assert.equal(round.strategyAudit.expectedEmotionAgentEnabled, true);
  assert.equal(round.p0Batch6Acceptance, true);
  assert.equal(round.stateSuite.requireEmotionStrategyCollision, true);
  assert.ok(round.discussionDurationMs > 5 * 60 * 1000);
  assert.deepEqual(round.stateSuite.scenarios.map((item) => item.id), ['S05', 'S06']);
  assert.equal(round.stateSuite.scenarios[0].shouldIntervene, true);
  assert.equal(round.stateSuite.scenarios[1].detectedSelfRegulation, true);
  assert.equal(round.stateSuite.scenarios[1].inhibitionStrategyId, 'OI-001');
  const interventionTriggerMessage = round.stateSuite.scenarios[0].messages.find(
    (message) => message.waitForExpectedInterventionAfter
  );
  assert.equal(interventionTriggerMessage.minimumPauseSeconds, 165);
  assert.equal(round.stateSuite.scenarios[1].messages[0].minimumGapBeforeSeconds, 30);
  assert.ok(
    round.scriptedDiscussion.messages[3].atSeconds +
      interventionTriggerMessage.minimumPauseSeconds < 5 * 60,
    'the recovery strategy window must start before the first five-minute emotion slot'
  );
});

test('one-group suites can retarget login, script, and audit metadata to G02', () => {
  const args = parseArgs([
    'node',
    'src/run.js',
    '--suite',
    'p0-batch6',
    '--target-group-code',
    'g02'
  ], {});
  const round = applyCliOverrides(suiteScenario, args);
  assert.equal(round.targetGroupCode, 'G02');
  assert.equal(round.stateSuite.groupCode, 'G02');
  assert.deepEqual(
    new Set(round.stateSuite.scenarios.map((item) => item.groupCode)),
    new Set(['G02'])
  );
  assert.deepEqual(
    new Set(round.scriptedDiscussion.scenarios.map((item) => item.groupCode)),
    new Set(['G02'])
  );
  assert.ok(round.scriptedDiscussion.messages.every((message) => (
    message.groupCode === 'G02' && /-G02-M\d+$/.test(message.studentId)
  )));
});

test('a missing discussion is represented only as an explicit empty audit baseline', () => {
  const baseline = buildEmptyStateSuiteBaseline({
    groupId: 2,
    sessionId: 1,
    taskId: 1
  });
  assert.equal(baseline.audit_available, true);
  assert.equal(baseline.empty_baseline_without_discussion, true);
  assert.deepEqual(baseline.scope, {
    group_id: 2,
    session_id: 1,
    discussion_id: null,
    task_id: 1
  });
  assert.equal(baseline.room_lock.locked, false);
  assert.ok(Object.values(baseline.tables).every((rows) => rows.length === 0));
  assert.ok(Object.values(baseline.available_tables).every(Boolean));
});

test('p0 batch6 direct mode writes the current discussion without claiming server audit', () => {
  const round = suiteScenario.forSuite('p0-batch6-direct');
  assert.equal(round.totalStudents, 4);
  assert.equal(round.p0Batch6Acceptance, true);
  assert.equal(round.p0Batch6Direct, true);
  assert.equal(round.strategyAudit.enabled, false);
  assert.equal(round.strategyAudit.requireActualCoverage, false);
  assert.equal(round.stateSuite.enabled, false);
  assert.equal(round.stateSuite.isolation, 'current_teacher_controlled_discussion');
  assert.equal(round.scriptedDiscussion.verificationMode, 'direct-observation-only');
  assert.ok(round.discussionDurationMs > 5 * 60 * 1000);
  assert.deepEqual(round.stateSuite.scenarios.map((item) => item.id), ['S05', 'S06']);
});

test('p0 batch6 audit computes zero hard failures and writes the exact nine artifacts', () => {
  const scenario = suiteScenario.forSuite('p0-batch6');
  const baseline = { groups: [{ dbAudit: { tables: {} } }] };
  const finalSnapshot = {
    groups: [{
      groupCode: 'G01',
      dbAudit: {
        scope: { group_id: 1, session_id: 2, discussion_id: 3 },
        room_lock: { locked: false, complete_lock_token_included: false },
        tables: {
          strategy_pipeline_runs: [
            {
              id: 1,
              should_intervene: 1,
              stage2_started_at: '2026-07-29T10:00:00Z',
              room_lock_acquired_at: '2026-07-29T10:00:00Z',
              room_lock_released_at: '2026-07-29T10:00:20Z',
              publish_status: 'PUBLISHED',
              published_message_id: 101,
              final_status: 'PUBLISHED'
            },
            {
              id: 2,
              should_intervene: 0,
              detected_self_regulation: 1,
              fresh_detected_self_regulation: 1,
              inhibition_strategy_id: 'OI-001',
              stage2_started_at: '2026-07-29T10:05:00Z',
              publish_status: 'NOT_PUBLISHED',
              final_status: 'SUPPRESSED'
            }
          ],
          emotion_reflection_slots: [{
            id: 10,
            status: 'sent',
            defer_count: 1,
            message_id: 102,
            coordination_strategy_run_id: 1
          }],
          messages: [
            { id: 100, agent_type: null, created_at: '2026-07-29T10:00:00Z' },
            { id: 101, agent_type: 'strategy', intervention_run_id: 21, created_at: '2026-07-29T10:00:20Z' },
            { id: 102, agent_type: 'emotion', intervention_run_id: 22, created_at: '2026-07-29T10:05:10Z' }
          ],
          strategy_pipeline_latency_events: [{ id: 30, event: 'stage2_started' }]
        }
      },
      teacherApis: { status: 'safe-test-snapshot' }
    }]
  };
  const summary = { auditAvailable: true, actualServerCoveragePassed: true };
  const acceptance = buildP0Batch6Acceptance({
    scenario,
    summary,
    baseline,
    finalSnapshot,
    events: [],
    errors: []
  });
  assert.deepEqual(Object.values(acceptance.hard_metrics), Array(8).fill(0));
  assert.equal(acceptance.emotion_outcome.deferred_then_sent, true);
  assert.equal(acceptance.passed, true);

  const reportDir = fs.mkdtempSync(path.join(os.tmpdir(), 'p0-batch6-report-'));
  try {
    const bundle = writeP0Batch6Bundle({
      reportDir,
      runId: 'p0-batch6-test',
      scenario,
      summary,
      events: [],
      errors: [],
      transcripts: [],
      strategyAuditSnapshots: { baseline, final: finalSnapshot },
      acceptance
    });
    assert.deepEqual(
      bundle.files.map((file) => path.basename(file)).sort(),
      [
        'summary.json', 'events.jsonl', 'errors.jsonl', 'transcript.json',
        'strategy-audit.json', 'emotion-slot-audit.json', 'room-lock-audit.json',
        'three-stage-latency.json', 'teacher-api-snapshot.json'
      ].sort()
    );
    assert.ok(bundle.files.every((file) => fs.existsSync(file)));
  } finally {
    fs.rmSync(reportDir, { recursive: true, force: true });
  }
});

test('the 144 messages are sequential and reserve 55 minutes for live Agent locks', () => {
  const messages = suiteScenario.scriptedDiscussion.messages;
  const firstMessageTimes = suiteScenario.scriptedDiscussion.scenarios.map((item) => (
    messages.find((message) => message.scenarioId === item.id).atSeconds
  ));
  for (let index = 1; index < firstMessageTimes.length; index += 1) {
    const previousScenarioId = suiteScenario.scriptedDiscussion.scenarios[index - 1].id;
    const previousLastMessage = messages.filter((message) => message.scenarioId === previousScenarioId).at(-1);
    assert.ok(firstMessageTimes[index] > previousLastMessage.atSeconds);
  }
  const lastMessage = messages.at(-1);
  assert.ok(lastMessage.atSeconds * 1000 < suiteScenario.discussionDurationMs);
  assert.ok(lastMessage.atSeconds <= 5 * 60);
  assert.ok(suiteScenario.discussionDurationMs - lastMessage.atSeconds * 1000 >= 55 * 60 * 1000);
  assert.equal(messages.length, 144);
  assert.equal(suiteScenario.discussionDurationMs, 60 * 60 * 1000);
  assert.equal(messages.filter((message) => message.waitForExpectedInterventionAfter).length, 9);
  assert.equal(messages.filter((message) => message.waitForNoInterventionAfter).length, 7);
});

test('summary reports unsent scripted messages against the complete plan', () => {
  const metrics = new Metrics({ runId: 'summary-progress', scenario: suiteScenario });
  assert.deepEqual(metrics.summary().scriptedMessageProgress, {
    planned: 144,
    attempted: 0,
    successful: 0,
    failed: 0,
    unsent: 144,
    incomplete: 144,
    complete: false
  });
});

test('every state case is independently runnable by ID and canonical name', () => {
  for (const item of suiteScenario.scriptedDiscussion.scenarios) {
    const round = suiteScenario.forStateCase(item.id);
    const byName = suiteScenario.forStateCase(stateCaseName(item));
    assert.equal(round.totalStudents, 4);
    assert.equal(round.groupCount, 1);
    assert.equal(round.scriptedDiscussion.scenarios.length, 1);
    assert.equal(round.scriptedDiscussion.scenarios[0].id, item.id);
    assert.equal(byName.scriptedDiscussion.scenarios[0].id, item.id);
    assert.match(round.name, new RegExp(`case-${item.id.toLowerCase()}$`));
    assert.equal(round.strategyAudit.requireCleanContext, true);
    assert.ok(round.scriptedDiscussion.messages.every((message) => message.groupCode === 'G01'));
    assert.ok(new Set(round.scriptedDiscussion.messages.map((message) => message.memberNo)).size >= 2);
    assert.doesNotThrow(() => assertDryRunStrategyCoverage(round));
  }
});

test('primary suite can resume at a failed state without replaying earlier cases', () => {
  const resumed = applyCliOverrides(suiteScenario, { fromStateCase: 'S11' });
  assert.deepEqual(
    resumed.scriptedDiscussion.scenarios.map((item) => item.id),
    ['S11', 'S12', 'S13', 'S14', 'S15', 'S16']
  );
  assert.equal(resumed.stateSuite.resumeFromScenarioId, 'S11');
  assert.equal(resumed.stateSuite.includeRecoveryBridges, false);
  assert.ok(resumed.scriptedDiscussion.messages.some((message) => message.scenarioId === 'S11'));
  assert.ok(resumed.scriptedDiscussion.messages.some((message) => message.scenarioId === 'S12'));
  assert.ok(!resumed.scriptedDiscussion.messages.some((message) => message.phase === 'recovery'));
});

test('all 16 independent cases require their matching server-detected state and overlays', () => {
  for (const item of suiteScenario.scriptedDiscussion.scenarios) {
    const round = suiteScenario.forStateCase(item.id);
    const coverage = buildActualStateCoverage(
      round,
      auditSnapshot(item, 'baseline'),
      auditSnapshot(item, 'final')
    );
    assert.equal(coverage.passed, true, item.id);
    const mismatch = buildActualStateCoverage(
      round,
      auditSnapshot(item, 'baseline'),
      auditSnapshot(item, 'final', (run) => ({ ...run, canonical_sub_state_code: 'standard', final_sub_state_code: 'standard' }))
    );
    assert.equal(mismatch.passed, item.canonicalSubState === 'standard', item.id);
  }
});

test('npm config forwarding selects one state case instead of silently running all 16', () => {
  const args = parseArgs(
    ['node', 'src/run.js'],
    {
      npm_config_state_case: 'standard',
      npm_config_base_url: 'http://127.0.0.1:5000/'
    }
  );
  const selected = applyCliOverrides(suiteScenario, args);
  assert.equal(selected.scriptedDiscussion.scenarios.length, 1);
  assert.equal(selected.scriptedDiscussion.scenarios[0].id, 'S02');
  assert.equal(selected.baseUrl, 'http://127.0.0.1:5000/');
  const positionalArgs = parseArgs(
    ['node', 'src/run.js', 'standard', 'http://127.0.0.1:5000/'],
    {}
  );
  assert.equal(positionalArgs.stateCase, 'standard');
  const suiteArgs = parseArgs(['node', 'src/run.js', 'overlays'], {});
  assert.equal(suiteArgs.suite, 'overlays');
});

test('standard avoids execution assignment and deep thinking keeps an explicit analysis pause', () => {
  const standard = suiteScenario.forStateCase('S02').scriptedDiscussion.messages
    .map((message) => message.text)
    .join('\n');
  assert.doesNotMatch(standard, /我负责|我来写|你整理|分工|交给我|A写|B写|C补/);
  const deep = suiteScenario.forStateCase('deep_thinking').scriptedDiscussion.messages;
  assert.ok(deep.some((message, index) => (
    index > 0 && message.atSeconds - deep[index - 1].atSeconds >= 75
  )));
  const deepText = deep.map((message) => message.text).join('\n');
  assert.match(deepText, /算一下|对照一下|大家先等我/);
  assert.match(deepText, /算好了|方案|噪声控制/);
  assert.doesNotMatch(deepText, /我负责|交给我/);
});

test('S11 keeps explicit value-loss evidence before the intervention boundary', () => {
  const burnout = suiteScenario.scriptedDiscussion.scenarios.find((item) => item.id === 'S11');
  const triggerText = burnout.messages
    .filter((message) => message.phase !== 'continuation')
    .map((message) => message.text)
    .join('\n');

  assert.match(triggerText, /不一定真的采用|模拟任务/);
  assert.match(triggerText, /没有意义|不想再比较/);
  assert.equal(burnout.canonicalSubState, 'burnout');
  assert.deepEqual(burnout.allowedStrategyIds, ['ER-008']);
});

test('script labels explicitly do not claim server recognition', () => {
  const round = suiteScenario.forStateCase('S01');
  const events = round.scriptedDiscussion.messages.map((message, index) => ({
    type: 'message_success',
    studentId: message.studentId,
    seq: index + 1,
    kind: `script:${message.state}`
  }));
  const coverage = buildScriptedStateCoverage(round, events);
  assert.equal(coverage.coverageSource, 'script_markers_only');
  assert.equal(coverage.provesServerDetection, false);
});

test('server audit accepts one allowed strategy route without claiming all candidates', () => {
  const stateScenario = suiteScenario.scriptedDiscussion.scenarios.find((item) => item.id === 'S07');
  const roundScenario = suiteScenario.forStateCase(stateScenario.id);
  const coverage = buildActualStrategyCoverage(
    roundScenario,
    auditSnapshot(stateScenario, 'baseline'),
    auditSnapshot(stateScenario, 'final', (run) => ({
      ...run,
      selected_strategy_id: 'ER-001',
      strategy_candidate_ids: ['ER-001'],
      supporting_strategy_ids: []
    }))
  );
  assert.equal(coverage.passed, true);
  assert.equal(
    coverage.routeAcceptance,
    'selected_strategy_id must be in the authoritative primary+overlay runtime route; ' +
      'expected_primary_strategy_id is diagnostic unless exact_strategy_required is set; ' +
      'candidate-only is accepted only for non-intervening states without OI'
  );
  assert.deepEqual(coverage.scenarios[0].observedStrategyIds, ['ER-001']);
  assert.ok(coverage.scenarios[0].missingStrategyIds.length > 0);
  const candidateOnly = buildActualStrategyCoverage(
    roundScenario,
    auditSnapshot(stateScenario, 'baseline'),
    auditSnapshot(stateScenario, 'final', (run) => ({
      ...run,
      selected_strategy_id: null,
      supporting_strategy_ids: [],
      strategy_candidate_ids: ['ER-001']
    }))
  );
  assert.equal(candidateOnly.passed, false);
});

test('a backup strategy remains valid without matching the expected primary strategy', () => {
  const stateScenario = suiteScenario.scriptedDiscussion.scenarios.find((item) => item.id === 'S07');
  const roundScenario = suiteScenario.forStateCase(stateScenario.id);
  const coverage = buildActualStrategyCoverage(
    roundScenario,
    auditSnapshot(stateScenario, 'baseline'),
    auditSnapshot(stateScenario, 'final', (run) => ({
      ...run,
      selected_strategy_id: 'EE-001',
      strategy_candidate_ids: ['EE-001'],
      supporting_strategy_ids: []
    }))
  );
  const result = coverage.scenarios[0];

  assert.equal(result.expectedPrimaryStrategyId, 'ER-001');
  assert.equal(result.selectedStrategyId, 'EE-001');
  assert.equal(result.routeValid, true);
  assert.equal(result.exactPrimaryMatched, false);
  assert.equal(result.passed, true);
});

test('a selected strategy outside the authoritative route fails the scenario', () => {
  const stateScenario = suiteScenario.scriptedDiscussion.scenarios.find((item) => item.id === 'S07');
  const roundScenario = suiteScenario.forStateCase(stateScenario.id);
  const coverage = buildActualStrategyCoverage(
    roundScenario,
    auditSnapshot(stateScenario, 'baseline'),
    auditSnapshot(stateScenario, 'final', (run) => ({
      ...run,
      selected_strategy_id: 'EA-999',
      strategy_candidate_ids: ['EA-999'],
      supporting_strategy_ids: []
    }))
  );
  const result = coverage.scenarios[0];

  assert.equal(result.routeValid, false);
  assert.equal(result.passed, false);
});

test('server state and overlay coverage come from audit runs, not script markers', () => {
  const overlaySuite = suiteScenario.forSuite('overlays');
  const stateScenario = overlaySuite.scriptedDiscussion.scenarios[0];
  const oneOverlay = {
    ...overlaySuite,
    stateSuite: { ...overlaySuite.stateSuite, scenarios: [stateScenario] },
    scriptedDiscussion: {
      ...overlaySuite.scriptedDiscussion,
      scenarios: [stateScenario],
      messages: overlaySuite.scriptedDiscussion.messages.filter(
        (message) => message.scenarioId === stateScenario.id
      )
    }
  };
  const coverage = buildActualStateCoverage(
    oneOverlay,
    auditSnapshot(stateScenario, 'baseline'),
    auditSnapshot(stateScenario, 'final')
  );
  assert.equal(coverage.coverageSource, 'teacher_agent_audit_strategy_pipeline_runs');
  assert.equal(coverage.passed, true);
  const missingOverlay = buildActualStateCoverage(
    oneOverlay,
    auditSnapshot(stateScenario, 'baseline'),
    auditSnapshot(stateScenario, 'final', (run) => ({ ...run, secondary_sub_state_tags: [] }))
  );
  assert.equal(missingOverlay.passed, false);
});

test('model-failure suite requires explicit unclassified fallback and no published intervention', () => {
  const failureSuite = suiteScenario.forSuite('model-failure');
  const stateScenario = failureSuite.scriptedDiscussion.scenarios[0];
  const baseline = strictAuditSnapshot(stateScenario, 'baseline');
  const final = strictAuditSnapshot(stateScenario, 'final', () => ({
    id: 201,
    final_status: 'FAILED',
    publish_status: 'NOT_PUBLISHED'
  }), {
    assessmentBatches: [{
      id: 9,
      terminal_status: 'quarantined',
      error_code: 'truncated_response',
      fallback_segment_count: 1,
      assignment_source: 'batch_unclassified'
    }]
  });
  const coverage = buildActualServerCoverage(
    failureSuite,
    baseline,
    final,
    scenarioEvents(stateScenario)
  );
  assert.equal(coverage.modelFailureCoverage.passed, true);
  assert.equal(coverage.actualStrategyCoverage, null);
  assert.equal(coverage.passed, true);
});

test('agent-lock suite requires emotion Agent isolation and terminal strategy runs', () => {
  const lockSuite = suiteScenario.forSuite('agent-lock');
  const stateScenario = lockSuite.scriptedDiscussion.scenarios[0];
  const oneScenario = {
    ...lockSuite,
    scriptedDiscussion: { ...lockSuite.scriptedDiscussion, scenarios: [stateScenario] }
  };
  const baseline = strictAuditSnapshot(
    stateScenario,
    'baseline',
    (run) => run,
    { emotionAgentEnabled: true }
  );
  const final = strictAuditSnapshot(
    stateScenario,
    'final',
    (run) => run,
    { emotionAgentEnabled: true }
  );
  final.groups[0].audit.strategy_pipeline_runs.push({
    id: 999,
    final_status: 'SUPERSEDED',
    publish_status: 'SKIPPED'
  });
  final.groups[0].dbAudit.tables.strategy_pipeline_runs.push({
    id: 1000,
    group_id: 1,
    session_id: 42,
    discussion_id: 77,
    task_id: 9,
    trigger_source: 'silence_check',
    assessment_batch_id: null,
    stage2_status: 'PENDING',
    stage2_started_at: null,
    room_lock_acquired_at: null,
    publish_status: 'NOT_READY',
    final_status: 'PENDING_STAGE2'
  });
  const coverage = buildActualServerCoverage(
    oneScenario,
    baseline,
    final,
    scenarioEvents(stateScenario)
  );
  assert.equal(coverage.agentLockCoverage.emotionAgentEnabled, true);
  assert.deepEqual(
    coverage.agentLockCoverage.groups[0].ignoredLockFreePreliminaryRunIds,
    ['1000']
  );
  assert.equal(coverage.agentLockCoverage.passed, true);
  assert.equal(coverage.passed, true);

  final.groups[0].dbAudit.tables.strategy_pipeline_runs.push({
    id: 1001,
    group_id: 1,
    session_id: 42,
    discussion_id: 77,
    task_id: 9,
    trigger_source: 'student_message',
    assessment_batch_id: 88,
    stage2_status: 'RUNNING',
    stage2_started_at: '2026-07-30 00:00:00',
    room_lock_acquired_at: '2026-07-30 00:00:00',
    publish_status: 'NOT_READY',
    final_status: 'ASSESSING'
  });
  const active = buildActualServerCoverage(
    oneScenario,
    baseline,
    final,
    scenarioEvents(stateScenario)
  );
  assert.deepEqual(active.agentLockCoverage.groups[0].activeRunIds, ['1001']);
  assert.equal(active.agentLockCoverage.passed, false);
});

test('state-suite emotion Agent preflight explains how to fix mismatched sessions', () => {
  const primary = suiteScenario.forSuite('primary-substates');
  assert.throws(
    () => assertAgentFlagCompatibility({
      scenario: primary,
      currentSession: { session_no: 1, status: 'running' },
      sessionId: 1,
      emotionEnabled: true
    }),
    (error) => {
      assert.match(error.message, /primary-substates/);
      assert.match(error.message, /requires emotion_agent_enabled=false/);
      assert.match(error.message, /session 1, no\.1, status=running reports true/);
      assert.match(error.message, /emotion Agent disabled/);
      assert.match(error.message, /--suite agent-lock/);
      assert.match(error.message, /--expected-session-id <id>/);
      return true;
    }
  );

  const lockSuite = suiteScenario.forSuite('agent-lock');
  assert.throws(
    () => assertAgentFlagCompatibility({
      scenario: lockSuite,
      currentSession: { session_no: 2, status: 'running' },
      sessionId: 2,
      emotionEnabled: false
    }),
    /agent-lock.*requires emotion_agent_enabled=true.*reports false/
  );

  assert.doesNotThrow(() => assertAgentFlagCompatibility({
    scenario: primary,
    currentSession: { session_no: 3, status: 'running' },
    sessionId: 3,
    emotionEnabled: false
  }));
});

test('canonical DB coverage allows the immediately preceding recovery or continuation bridge only', () => {
  const lockSuite = suiteScenario.forSuite('agent-lock');
  const stateScenario = lockSuite.scriptedDiscussion.scenarios.find((item) => item.id === 'L02');
  const oneScenario = {
    ...lockSuite,
    scriptedDiscussion: {
      ...lockSuite.scriptedDiscussion,
      scenarios: [stateScenario],
      messages: lockSuite.scriptedDiscussion.messages.filter((message) => (
        message.scenarioId === stateScenario.id || message.phase === 'recovery'
      ))
    }
  };
  const baseline = strictAuditSnapshot(stateScenario, 'baseline');
  const final = strictAuditSnapshot(stateScenario, 'final');
  const scope = final.groups[0].dbAudit.scope;
  final.groups[0].dbAudit.tables.collaboration_state_segments.push(
    {
      id: 302,
      ...scope,
      canonical_sub_state_code: 'standard',
      evidence_message_ids: [499],
      start_sequence: 9,
      end_sequence: 9,
      source: 'strategy_llm'
    },
    {
      id: 303,
      ...scope,
      canonical_sub_state_code: 'standard',
      evidence_message_ids: [499, 501],
      start_sequence: 9,
      end_sequence: 10,
      source: 'strategy_llm'
    }
  );
  const events = [
    {
      type: 'message_success',
      scenarioId: 'L01-RECOVERY',
      phase: 'continuation',
      messageId: 499
    },
    ...scenarioEvents(stateScenario)
  ];

  const accepted = buildCanonicalDbCoverage(oneScenario, baseline, final, events);
  assert.equal(accepted.scenarios[0].crossCaseRecordIds.length, 0);
  assert.equal(accepted.passed, true);

  final.groups[0].dbAudit.tables.collaboration_state_segments.push({
    id: 304,
    ...scope,
    canonical_sub_state_code: 'standard',
    evidence_message_ids: [498, 501],
    start_sequence: 8,
    end_sequence: 10,
    source: 'strategy_llm'
  });
  const rejected = buildCanonicalDbCoverage(oneScenario, baseline, final, events);
  assert.deepEqual(rejected.scenarios[0].crossCaseRecordIds, [304]);
  assert.equal(rejected.passed, false);
});

test('real coverage requires message IDs plus DB, teacher API, and export agreement', () => {
  const stateScenario = suiteScenario.scriptedDiscussion.scenarios.find((item) => item.id === 'S03');
  const round = suiteScenario.forStateCase(stateScenario.id);
  const events = scenarioEvents(stateScenario);
  const baseline = strictAuditSnapshot(stateScenario, 'baseline');
  const final = strictAuditSnapshot(stateScenario, 'final');
  const coverage = buildActualServerCoverage(round, baseline, final, events);
  assert.equal(coverage.validationMode, 'real_coverage');
  assert.equal(coverage.auditAvailable, true);
  assert.deepEqual(Object.keys(coverage.coverage), [
    'planned_script_coverage',
    'message_send_coverage',
    'canonical_db_coverage',
    'teacher_api_coverage',
    'export_coverage',
    'intervention_coverage',
    'inhibition_coverage'
  ]);
  assert.ok(Object.values(coverage.coverage).every((item) => item.passed));
  assert.equal(coverage.passed, true);

  const unavailable = {
    ...final,
    auditAvailable: false,
    groups: final.groups.map((group) => ({ ...group, exportAudit: null }))
  };
  const failed = buildActualServerCoverage(round, baseline, unavailable, events);
  assert.equal(failed.auditAvailable, false);
  assert.equal(failed.exportCoverage.passed, false);
  assert.equal(failed.passed, false);
});

test('real coverage reconciles DB sequence evidence with API message IDs', () => {
  const stateScenario = suiteScenario.scriptedDiscussion.scenarios.find((item) => item.id === 'S05');
  const round = suiteScenario.forStateCase(stateScenario.id);
  const events = scenarioEvents(stateScenario);
  const baseline = strictAuditSnapshot(stateScenario, 'baseline');
  const final = strictAuditSnapshot(stateScenario, 'final');
  const group = final.groups[0];
  const sequences = events.map((event) => event.seq);
  const run = group.dbAudit.tables.strategy_pipeline_runs[0];
  run.evidence_message_ids = [sequences[0]];
  run.evidence_sequences = [];
  group.audit.strategy_pipeline_runs[0].evidence_message_ids = [sequences[0]];
  group.audit.strategy_pipeline_runs[0].evidence_sequences = [sequences[0]];
  const segment = group.dbAudit.tables.collaboration_state_segments[0];
  segment.evidence_message_ids = sequences;
  segment.evidence_sequences = sequences;
  group.exportAudit.files['strategy_pipeline_runs.csv'][0].evidence_message_ids =
    JSON.stringify([sequences[0]]);

  const coverage = buildActualServerCoverage(round, baseline, final, events);

  assert.equal(coverage.canonicalDbCoverage.passed, true);
  assert.equal(coverage.teacherApiCoverage.passed, true);
  assert.equal(coverage.exportCoverage.passed, true);
  assert.equal(coverage.interventionCoverage.passed, true);
  assert.equal(coverage.passed, true);
});

test('canonical DB coverage does not treat distinct state-monitor windows as duplicates', () => {
  const stateScenario = suiteScenario.scriptedDiscussion.scenarios.find((item) => item.id === 'S02');
  const round = suiteScenario.forStateCase(stateScenario.id);
  const baseline = strictAuditSnapshot(stateScenario, 'baseline');
  const final = strictAuditSnapshot(stateScenario, 'final');
  const scope = final.groups[0].dbAudit.scope;
  final.groups[0].dbAudit.tables.collaboration_state_segments.push(
    {
      id: 901,
      ...scope,
      state_code: 'positive_collaboration',
      start_message_id: 504,
      end_message_id: 504,
      source: 'state_monitor',
      evidence_message_ids: [504],
      dedupe_key: 'state_monitor_assessment:901'
    },
    {
      id: 902,
      ...scope,
      state_code: 'positive_collaboration',
      start_message_id: 508,
      end_message_id: 508,
      source: 'state_monitor',
      evidence_message_ids: [508],
      dedupe_key: 'state_monitor_assessment:902'
    }
  );

  const coverage = buildActualServerCoverage(
    round,
    baseline,
    final,
    scenarioEvents(stateScenario)
  );

  assert.deepEqual(coverage.canonicalDbCoverage.scenarios[0].duplicateSegments, []);
  assert.equal(coverage.canonicalDbCoverage.passed, true);
  assert.equal(coverage.passed, true);
});

test('teacher group list accepts a resolved current state after detected self-regulation', () => {
  const stateScenario = suiteScenario.scriptedDiscussion.scenarios.find((item) => item.id === 'S06');
  const round = suiteScenario.forStateCase(stateScenario.id);
  const baseline = strictAuditSnapshot(stateScenario, 'baseline');
  const final = strictAuditSnapshot(stateScenario, 'final');
  const currentGroup = final.groups[0].teacherApis.teacherGroups.groups[0];
  currentGroup.final_sub_state_code = 'standard';
  currentGroup.assessment_status = 'confirmed';

  const coverage = buildActualServerCoverage(
    round,
    baseline,
    final,
    scenarioEvents(stateScenario)
  );

  assert.deepEqual(
    coverage.teacherApiCoverage.expectedCurrentStates,
    ['constructive_conflict', 'standard', 'execution_progress']
  );
  assert.equal(coverage.teacherApiCoverage.observedCurrentState, 'standard');
  assert.equal(coverage.teacherApiCoverage.groupListPassed, true);
  assert.equal(coverage.passed, true);
});

test('teacher group list accepts post-intervention observing before a follow-up message', () => {
  const stateScenario = suiteScenario.scriptedDiscussion.scenarios.find((item) => item.id === 'S10');
  const round = suiteScenario.forStateCase(stateScenario.id);
  const baseline = strictAuditSnapshot(stateScenario, 'baseline');
  const final = strictAuditSnapshot(stateScenario, 'final');
  const currentGroup = final.groups[0].teacherApis.teacherGroups.groups[0];
  currentGroup.final_sub_state_code = null;
  currentGroup.state_code = 'observing';
  currentGroup.assessment_status = 'observing';
  currentGroup.assignment_source = 'post_intervention_observation';

  const coverage = buildActualServerCoverage(
    round,
    baseline,
    final,
    scenarioEvents(stateScenario)
  );

  assert.deepEqual(
    coverage.teacherApiCoverage.expectedCurrentStates,
    ['frustration', 'observing']
  );
  assert.equal(coverage.teacherApiCoverage.observedCurrentState, 'observing');
  assert.equal(coverage.teacherApiCoverage.groupListPassed, true);
  assert.equal(coverage.passed, true);
});

test('all 16 independent cases satisfy the strict five-stage acceptance contract', () => {
  for (const stateScenario of suiteScenario.scriptedDiscussion.scenarios) {
    const round = suiteScenario.forStateCase(stateScenario.id);
    const coverage = buildActualServerCoverage(
      round,
      strictAuditSnapshot(stateScenario, 'baseline'),
      strictAuditSnapshot(stateScenario, 'final'),
      scenarioEvents(stateScenario)
    );
    assert.equal(
      coverage.passed,
      true,
      `${stateScenario.id}: ${JSON.stringify(
        Object.fromEntries(
          Object.entries(coverage.coverage).map(([key, value]) => [key, value.passed])
        )
      )}`
    );
  }
});

test('message send coverage rejects script plans without server message IDs', () => {
  const stateScenario = suiteScenario.scriptedDiscussion.scenarios[0];
  const round = suiteScenario.forStateCase(stateScenario.id);
  assert.equal(buildMessageSendCoverage(round, []).passed, false);
  const events = scenarioEvents(stateScenario).map((event) => ({
    ...event,
    messageId: null
  }));
  assert.equal(buildMessageSendCoverage(round, events).passed, false);
});

test('CSV export audit keeps only coverage fields and drops content and identities', () => {
  const csv = [
    'message_id,role,content,participant_code,final_sub_state_code,assignment_source',
    '11,student,"private, text",P0001,execution_progress,model_segment'
  ].join('\r\n');
  assert.equal(parseCsv(csv)[0].content, 'private, text');
  const zipEntries = {
    'S001/G01/messages.csv': Buffer.from(csv),
    'S001/G01/strategy_pipeline_runs.csv': Buffer.from('pipeline_run_id,canonical_sub_state_code\r\n1,execution_progress'),
    'S001/G01/interventions.csv': Buffer.from('intervention_run_id,canonical_sub_state_code\r\n'),
    'S001/G01/unified-events.csv': Buffer.from('event_type,final_sub_state_code\r\nstate_assignment,execution_progress')
  };
  const parsedEntries = readZipEntries(storedZip(zipEntries));
  const audit = buildExportAuditFromEntries(parsedEntries);
  assert.equal(audit.available, true);
  assert.equal(audit.files['messages.csv'][0].final_sub_state_code, 'execution_progress');
  assert.equal('content' in audit.files['messages.csv'][0], false);
  assert.equal('participant_code' in audit.files['messages.csv'][0], false);

  const safeAgent = sanitizeAgentAudit({
    message_timeline: [{
      id: 11,
      role: 'student',
      content: 'private text',
      display_name: 'participant'
    }],
    strategy_pipeline_runs: [{
      id: 12,
      detected_self_regulation: '是',
      generated_intervention_text: 'private generated text'
    }]
  });
  assert.equal('content' in safeAgent.message_timeline[0], false);
  assert.equal(
    safeAgent.strategy_pipeline_runs[0].detected_self_regulation,
    '是'
  );
  assert.equal(
    'generated_intervention_text' in safeAgent.strategy_pipeline_runs[0],
    false
  );
  assert.equal('display_name' in safeAgent.message_timeline[0], false);
});

test('all requested suites are selectable and dry-run-valid', () => {
  for (const name of ['primary-substates', 'overlays', 'model-failure', 'agent-lock', 'p0-batch6', 'p0-batch6-direct']) {
    const selected = suiteScenario.forSuite(name);
    assert.equal(selected.stateSuite.mode, name);
    assert.doesNotThrow(() => assertDryRunStrategyCoverage(selected));
  }
});
