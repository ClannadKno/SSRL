#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const { Metrics } = require('./metrics');
const { RunState } = require('./runState');
const {
  assertDryRunStateCoverage,
  expectedStatesForScenario,
  plannedStatesForScenario
} = require('./stateCoverage');
const {
  assertDryRunStrategyCoverage,
  expectedStrategyIdsForScenario
} = require('./strategyCoverage');
const {
  applyCliOverrides,
  createRunId,
  formatDuration,
  loadScenario,
  parseArgs,
  randomInt,
  readCsvRecords,
  readJson,
  showHelp,
  sleep
} = require('./utils');

async function main() {
  const args = parseArgs(process.argv);
  if (args.help || args.h) {
    console.log(showHelp());
    return;
  }

  const scenario = applyCliOverrides(loadScenario(args.scenario), args);
  const runId = args.runId ? validateRunId(args.runId) : createRunId(scenario.name);
  const students = loadStudents(scenario);
  const assignedStudents = assignProfiles(students.slice(0, scenario.totalStudents), scenario.profiles);
  const auditor = loadAuditor(scenario);
  attachScriptedDiscussion(scenario, assignedStudents);
  const groupPlans = createGroupPlans(assignedStudents, scenario);

  printPlan(scenario, assignedStudents, groupPlans, runId);

  if (args.dryRun) {
    validateDryRun(scenario, assignedStudents, groupPlans, auditor);
    const auditEnabled = Boolean(scenario.strategyAudit && scenario.strategyAudit.enabled);
    console.log(auditEnabled
      ? '[dry-run] validation_mode=plan_only; config and student data look usable; DB/API/export audit was intentionally skipped.'
      : '[dry-run] validation_mode=student_discussion_only; config and student data look usable; teacher/session audit is not part of this run.');
    return;
  }

  validateActualAuditConfiguration(scenario);
  ensureRuntimeDependencies();

  const metrics = new Metrics({ runId, scenario });
  const runState = new RunState({
    runId,
    totalStudents: scenario.totalStudents,
    minReadyStudents: scenario.minReadyStudents,
    groupPlans,
    scriptedMessages: (
      scenario.scriptedDiscussion && scenario.scriptedDiscussion.messages
    ) || []
  });

  let browser = null;
  let report = null;
  const studentPromises = [];
  let watchdog = null;
  let strategyAuditBaseline = null;
  let strategyAuditSession = null;

  const stop = (reason) => {
    runState.requestStop(reason);
    if (!runState.started) runState.start(0);
  };

  process.once('SIGINT', () => {
    console.log('\n[main] SIGINT received, stopping test...');
    stop('SIGINT');
  });

  try {
    const { launchSharedBrowser } = require('./browser');
    const { runStudent } = require('./student');
    const {
      assertCleanStrategyAuditBaseline,
      captureStrategyAuditSnapshot,
      closeStrategyAuditSession,
      createStrategyAuditSession
    } = require('./strategyAudit');

    browser = await launchSharedBrowser(scenario);

    if (scenario.strategyAudit && scenario.strategyAudit.enabled) {
      console.log('[audit] Capturing clean-context baseline for all scripted groups.');
      strategyAuditSession = await createStrategyAuditSession({
        browser,
        scenario,
        auditor
      });
      strategyAuditBaseline = await captureStrategyAuditSnapshot({
        browser,
        scenario,
        auditor,
        students: assignedStudents,
        phase: 'baseline',
        auditSession: strategyAuditSession
      });
      metrics.recordStrategyAuditSnapshot('baseline', strategyAuditBaseline);
      assertActualAuditAvailable(scenario, strategyAuditBaseline, 'baseline');
      assertCleanStrategyAuditBaseline(scenario, strategyAuditBaseline);
    }

    watchdog = setInterval(() => {
      const reason = metrics.getStopReason(scenario.stopConditions);
      if (reason) {
        console.error(`[watchdog] ${reason}`);
        stop(reason);
      }
    }, 5000);

    await rampUpStudents({
      browser,
      students: assignedStudents,
      scenario,
      metrics,
      runState,
      studentPromises,
      runStudent
    });

    if (!runState.stopRequested) {
      await waitForWarmup(runState, scenario, assignedStudents, metrics);
    }

    if (runState.stopRequested) {
      console.warn(`[main] Test stopped before discussion start: ${runState.stopReason}`);
      runState.start(0);
    } else {
      console.log(`[main] Starting discussion window: ${formatDuration(scenario.discussionDurationMs)}.`);
      runState.start(scenario.discussionDurationMs);
      if (scenario.flow && scenario.flow.stopAfterScriptedMessagesComplete) {
        const postDialogueWaitMs = Math.max(
          0,
          Number(
            scenario.flow.scriptedPostDialogueWaitMs ||
            (scenario.stateSuite && scenario.stateSuite.postDialogueWaitMs) ||
            0
          )
        );
        void runState.waitForAllScriptedMessagesComplete().then(async (completed) => {
          if (!completed || runState.stopRequested) return;
          console.log(
            `[main] All scripted messages drained; keeping students online for ${formatDuration(postDialogueWaitMs)}.`
          );
          if (postDialogueWaitMs) await sleep(postDialogueWaitMs);
          if (!runState.stopRequested) runState.finishDiscussionWindow();
        });
      }
    }

    await Promise.allSettled(studentPromises);

    const inputIntegrity = metrics.finalizeScriptedMessageInput();
    if (!inputIntegrity.complete) {
      console.error(
        `[main] ${inputIntegrity.status}: expected ${inputIntegrity.expected_script_messages} ` +
        `script messages, successfully sent ${inputIntegrity.successful_script_messages}.`
      );
      runState.requestStop('INPUT_INCOMPLETE');
    }

    if (scenario.strategyAudit && scenario.strategyAudit.enabled && !runState.stopRequested) {
      const settleMs = Math.max(0, Number(scenario.strategyAudit.settleMs || 0));
      if (settleMs) {
        console.log(`[audit] Waiting ${formatDuration(settleMs)} for state/strategy pipelines to settle.`);
        await sleep(settleMs);
      }
      const maxWaitMs = Math.max(0, Number(scenario.strategyAudit.maxWaitMs || 0));
      const pollIntervalMs = Math.max(1000, Number(scenario.strategyAudit.pollIntervalMs || 5000));
      const auditDeadline = Date.now() + maxWaitMs;
      let actualCoverage = null;
      do {
        console.log('[audit] Capturing final server-side state and strategy routes.');
        const finalSnapshot = await captureStrategyAuditSnapshot({
          browser,
          scenario,
          auditor,
          students: assignedStudents,
          phase: 'final',
          auditSession: strategyAuditSession
        });
        metrics.recordStrategyAuditSnapshot('final', finalSnapshot);
        assertActualAuditAvailable(scenario, finalSnapshot, 'final');
        actualCoverage = metrics.actualServerCoverage();
        const p0Acceptance = scenario.p0Batch6Acceptance
          ? metrics.p0Batch6Acceptance()
          : null;
        if (actualCoverage && actualCoverage.passed && (!p0Acceptance || p0Acceptance.passed)) break;
        if (Date.now() >= auditDeadline) break;
        const p0Gap = p0Acceptance && !p0Acceptance.passed
          ? `; P0 emotion outcome=${p0Acceptance.emotion_outcome.passed ? 'passed' : 'pending'}`
          : '';
        console.log(
          `[audit] Coverage is not complete yet (${formatServerCoverageGap(actualCoverage)}${p0Gap}); polling again.`
        );
        await sleep(Math.min(pollIntervalMs, Math.max(0, auditDeadline - Date.now())));
      } while (Date.now() <= auditDeadline);
      if (scenario.strategyAudit.requireActualCoverage && (!actualCoverage || !actualCoverage.passed)) {
        throw new Error(
          `Server-side state-suite coverage failed: ${formatServerCoverageGap(actualCoverage)}.`
        );
      }
    }
  } catch (error) {
    metrics.error('run_fatal_error', null, error);
    throw error;
  } finally {
    if (watchdog) clearInterval(watchdog);
    stop(runState.stopReason || 'finished');
    if (strategyAuditSession) {
      const { closeStrategyAuditSession } = require('./strategyAudit');
      await closeStrategyAuditSession(strategyAuditSession);
    }
    if (browser) await browser.close().catch(() => null);
    report = metrics.writeReports(scenario.reportDir);
    printSummary(report.summary, report);
  }
  if (
    scenario.p0Batch6Acceptance && !scenario.p0Batch6Direct &&
    (!report || !report.summary.p0Batch6Acceptance || !report.summary.p0Batch6Acceptance.passed)
  ) {
    throw new Error(
      'P0 batch 6 acceptance failed; inspect the nine-file report bundle for hard metrics and Agent outcomes.'
    );
  }
}

function ensureRuntimeDependencies() {
  try {
    require.resolve('playwright');
  } catch (_error) {
    throw new Error(
      'Playwright is not installed. Run these commands first:\n' +
      '  cd D:\\workspace\\nqy\\load-test\n' +
      '  npm install\n' +
      '  npm run install:browsers'
    );
  }
}

function loadStudents(scenario) {
  const filePath = path.resolve(scenario.studentsFile);
  if (!fs.existsSync(filePath)) {
    const examplePath = path.resolve(__dirname, '..', 'data', 'students.example.json');
    throw new Error(
      `Student file not found: ${filePath}\n` +
      `Create it from ${examplePath} and provide ${scenario.totalStudents} real login keys.`
    );
  }

  if (filePath.toLowerCase().endsWith('.csv')) {
    return loadStudentsFromCsv(filePath, scenario);
  }

  const raw = readJson(filePath);
  const students = Array.isArray(raw) ? raw : raw.students;
  if (!Array.isArray(students)) {
    throw new Error('Student file must be a JSON array or an object with a students array.');
  }
  if (students.length < scenario.totalStudents) {
    throw new Error(`Need ${scenario.totalStudents} students, but ${filePath} contains ${students.length}.`);
  }

  const normalized = students.map((student, index) => ({
    id: student.id || `student_${String(index + 1).padStart(3, '0')}`,
    loginKey: student.loginKey || student.login_key || student.key,
    group_code: student.group_code || student.groupCode,
    group_no: normalizeNumber(student.group_no || student.groupNo),
    member_no: normalizeNumber(student.member_no || student.memberNo),
    group_id: normalizeNumber(student.group_id || student.groupId),
    ...student
  }));
  const selected = scenario.targetGroupCode
    ? normalized.filter((student) => (
      String(student.group_code || '').trim().toUpperCase() === scenario.targetGroupCode
    ))
    : normalized;
  if (selected.length < scenario.totalStudents) {
    throw new Error(
      `Need ${scenario.totalStudents} students for ${scenario.targetGroupCode || 'the scenario'}, ` +
      `but ${filePath} contains ${selected.length}.`
    );
  }
  return selected.slice(0, scenario.totalStudents);
}

function loadStudentsFromCsv(filePath, scenario) {
  const records = readCsvRecords(filePath)
    .filter((row) => String(row.role || '').trim().toLowerCase() === 'student')
    .map((row) => ({
      ...row,
      id: row.participant_code || row.username || row.key_name,
      loginKey: row.login_key,
      group_code: row.group_code,
      group_no: normalizeNumber(row.group_no),
      member_no: normalizeNumber(row.member_no),
      group_id: normalizeNumber(row.group_id),
      user_id: normalizeNumber(row.user_id)
    }))
    .filter((row) => scenario.targetGroupCode
      ? String(row.group_code || '').trim().toUpperCase() === scenario.targetGroupCode
      : (row.group_no >= 1 && row.group_no <= scenario.groupCount))
    .filter((row) => row.member_no >= 1 && row.member_no <= scenario.membersPerGroup)
    .sort((a, b) => (a.group_no - b.group_no) || (a.member_no - b.member_no));

  if (records.length < scenario.totalStudents) {
    throw new Error(
      `Need ${scenario.totalStudents} students from ${filePath}, but only found ${records.length} ` +
      `matching role=student/${scenario.targetGroupCode
        ? `group_code=${scenario.targetGroupCode}`
        : `group_no<=${scenario.groupCount}`}/member_no<=${scenario.membersPerGroup}.`
    );
  }

  return records.slice(0, scenario.totalStudents);
}

function assignProfiles(students, profiles) {
  const names = Object.keys(profiles);
  const plannedCounts = {};
  let assignedCount = 0;

  for (const name of names) {
    plannedCounts[name] = Math.floor(students.length * profiles[name].ratio);
    assignedCount += plannedCounts[name];
  }

  let idx = 0;
  while (assignedCount < students.length) {
    plannedCounts[names[idx % names.length]] += 1;
    assignedCount += 1;
    idx += 1;
  }

  const result = [];
  for (const name of names) {
    for (let i = 0; i < plannedCounts[name]; i += 1) {
      const student = students[result.length];
      result.push({
        ...student,
        profileName: name,
        profile: profiles[name]
      });
    }
  }
  return shuffle(result);
}

function createGroupPlans(students, scenario) {
  const byGroup = groupStudents(students);
  const plans = {};
  for (const [groupCode, members] of Object.entries(byGroup)) {
    const actions = [];
    const checkinCount = randomRangeFromTuple(scenario.flow.groupCheckins || [0, 0]);
    const helpCount = randomRangeFromTuple(scenario.flow.groupHelps || [0, 0]);

    actions.push(...scriptedGroupActionsForGroup(groupCode, scenario));

    scheduleOffsets(checkinCount, scenario.discussionDurationMs, 0.18, 0.82, 45 * 1000)
      .forEach((offsetMs, index) => {
        const kind = index % 2 === 0 ? 'mid' : 'event';
        actions.push({
          id: `${groupCode}-checkin-${index + 1}`,
          type: 'checkin',
          checkinType: kind,
          offsetMs,
          note: `Load test ${kind} state ${index + 1} for ${groupCode}`
        });
      });

    scheduleOffsets(helpCount, scenario.discussionDurationMs, 0.32, 0.72, scenario.flow.helpMinGapMs || 75 * 1000)
      .forEach((offsetMs, index) => {
        actions.push({
          id: `${groupCode}-help-${index + 1}`,
          type: 'help',
          offsetMs,
          text: index === 0
            ? '我们对任务要求和下一步分工有点不确定，请给一个推进建议。'
            : '我们已经有几个方案，但难以比较优先级，请帮助我们梳理判断标准。'
        });
      });

    actions.sort((a, b) => a.offsetMs - b.offsetMs);
    plans[groupCode] = {
      groupCode,
      memberIds: members.map((member) => member.id),
      actions,
      submitted: false,
      submissionClaimedBy: null
    };
  }
  return plans;
}

async function rampUpStudents({ browser, students, scenario, metrics, runState, studentPromises, runStudent }) {
  const batchSize = scenario.rampUp.batchSize;
  const launchOrder = scenario.rampUp.byGroup
    ? Object.entries(groupStudents(students))
      .sort(([left], [right]) => left.localeCompare(right, undefined, { numeric: true }))
      .flatMap(([, members]) => members)
    : students;
  for (let i = 0; i < launchOrder.length; i += batchSize) {
    if (runState.stopRequested) break;
    const batch = launchOrder.slice(i, i + batchSize);
    console.log(`[ramp-up] Launching students ${i + 1}-${i + batch.length}/${launchOrder.length}.`);

    for (const student of batch) {
      if (runState.stopRequested) break;
      studentPromises.push(runStudent({
        browser,
        student,
        scenario,
        metrics,
        runState
      }));
    }

    if (i + batchSize < launchOrder.length) {
      await sleep(scenario.rampUp.intervalMs);
    }
  }
}

async function waitForWarmup(runState, scenario, students = [], metrics = null) {
  const startedAt = Date.now();
  while (!runState.stopRequested && !runState.hasEnoughEntered() && Date.now() - startedAt < scenario.maxWarmupWaitMs) {
    console.log(
      `[warmup] Entered discussion: ${runState.enteredCount()}/${scenario.totalStudents}.` +
      formatWarmupLaggingStudents(runState, students, metrics)
    );
    await sleep(5000);
  }

  const entered = runState.enteredCount();
  if (entered < scenario.minReadyStudents) {
    console.warn(
      `[warmup] Only ${entered}/${scenario.totalStudents} students entered before timeout; ` +
      `minimum requested was ${scenario.minReadyStudents}. Starting with available students.`
    );
    console.warn(`[warmup] Missing students:${formatWarmupLaggingStudents(runState, students, metrics) || ' none'}`);
  } else {
    console.log(`[warmup] Required students entered: ${entered}/${scenario.totalStudents}.`);
  }
}

function formatWarmupLaggingStudents(runState, students = [], metrics = null) {
  if (!Array.isArray(students) || !students.length || !metrics || !metrics.students) return '';
  const missing = students
    .filter((student) => !runState.hasEntered(student.id))
    .map((student) => {
      const state = metrics.students.get(student.id) || {};
      const phase = state.fatalError
        ? `fatal:${String(state.fatalError).slice(0, 60)}`
        : (state.lastPhase || 'not_started');
      return `${student.id}(${phase})`;
    });
  if (!missing.length) return '';
  return ` Missing: ${missing.slice(0, 8).join(', ')}${missing.length > 8 ? ', ...' : ''}`;
}

function validateDryRun(scenario, students, groupPlans, auditor) {
  if (!['light', 'full'].includes(scenario.resourceMode)) {
    throw new Error(`Invalid resourceMode: ${scenario.resourceMode}`);
  }
  const missingKeys = students.filter((student) => !student.loginKey).map((student) => student.id);
  if (missingKeys.length) {
    throw new Error(`Students missing loginKey: ${missingKeys.slice(0, 10).join(', ')}`);
  }

  if (scenario.flow.fullFlow) {
    const byGroup = groupStudents(students);
    const groupEntries = Object.entries(byGroup);
    if (groupEntries.length !== scenario.groupCount) {
      throw new Error(`Expected ${scenario.groupCount} groups, found ${groupEntries.length}.`);
    }
    const badGroups = groupEntries
      .filter(([, members]) => members.length !== scenario.membersPerGroup)
      .map(([groupCode, members]) => `${groupCode}:${members.length}`);
    if (badGroups.length) {
      throw new Error(`Expected ${scenario.membersPerGroup} members per group. Bad groups: ${badGroups.join(', ')}`);
    }
    const plannedCheckins = Object.values(groupPlans).reduce((sum, plan) => sum + plan.actions.filter((a) => a.type === 'checkin').length, 0);
    const plannedHelps = Object.values(groupPlans).reduce((sum, plan) => sum + plan.actions.filter((a) => a.type === 'help').length, 0);
    if (!scenario.scriptedDiscussion && (plannedCheckins < scenario.groupCount * 2 || plannedHelps < scenario.groupCount)) {
      throw new Error('Group action plan is unexpectedly sparse.');
    }
  }

  if (scenario.scriptedDiscussion && scenario.scriptedDiscussion.messages) {
    const knownStudents = new Set(students.map((student) => student.id));
    const unknown = scenario.scriptedDiscussion.messages
      .filter((message) => !knownStudents.has(message.studentId))
      .map((message) => message.studentId);
    if (unknown.length) {
      throw new Error(`Scripted discussion references unknown students: ${[...new Set(unknown)].join(', ')}`);
    }
    const lastMessage = scenario.scriptedDiscussion.messages[scenario.scriptedDiscussion.messages.length - 1];
    if (lastMessage && lastMessage.offsetMs > scenario.discussionDurationMs) {
      throw new Error(
        `Scripted discussion last message is scheduled at ${formatDuration(lastMessage.offsetMs)}, ` +
        `after the discussion duration ${formatDuration(scenario.discussionDurationMs)}.`
      );
    }

    const expectedStates = scenario.scriptedDiscussion.expectedRuleStates || [];
    if (expectedStates.length) {
      const scriptedStates = new Set(scenario.scriptedDiscussion.messages.map((message) => message.state).filter(Boolean));
      const missingStates = expectedStates
        .map((item) => item.state || item)
        .filter((state) => state && !scriptedStates.has(state));
      if (missingStates.length) {
        throw new Error(`Scripted discussion is missing expected state labels: ${missingStates.join(', ')}`);
      }
    }

    assertDryRunStateCoverage(scenario);
    assertDryRunStrategyCoverage(scenario);
  }

  if (scenario.strategyAudit && scenario.strategyAudit.enabled && (!auditor || !auditor.loginKey)) {
    throw new Error('Strategy audit is enabled but no teacher login key was found in the configured login file.');
  }

  for (const plan of Object.values(groupPlans)) {
    const outOfRange = plan.actions
      .filter((action) => action.offsetMs > scenario.discussionDurationMs)
      .map((action) => `${plan.groupCode}:${action.id}@${formatDuration(action.offsetMs)}`);
    if (outOfRange.length) {
      throw new Error(`Group actions scheduled after discussion end: ${outOfRange.join(', ')}`);
    }
  }
}

function validateActualAuditConfiguration(scenario) {
  if (!(scenario && scenario.stateSuite && scenario.stateSuite.enabled)) return;
  const audit = scenario.strategyAudit || {};
  if (!audit.enabled || !audit.requireActualCoverage) {
    throw new Error(
      'State-suite live runs require strategyAudit.enabled=true and ' +
      'strategyAudit.requireActualCoverage=true. Use --dry-run for plan_only validation.'
    );
  }
}

function assertActualAuditAvailable(scenario, snapshot, phase) {
  if (!(scenario.strategyAudit && scenario.strategyAudit.requireActualCoverage)) return;
  if (!snapshot || !snapshot.auditAvailable) {
    throw new Error(
      `Required ${phase} DB/API/export audit is unavailable. ` +
      'Enable SSRL_ENABLE_STATE_SUITE_AUDIT=1 on the isolated test server ' +
      'and verify all teacher/export endpoints before rerunning.'
    );
  }
}

function printPlan(scenario, students, groupPlans, runId) {
  const counts = students.reduce((acc, student) => {
    acc[student.profileName] = (acc[student.profileName] || 0) + 1;
    return acc;
  }, {});
  const byGroup = groupStudents(students);
  const groupCounts = Object.fromEntries(Object.entries(byGroup).map(([key, members]) => [key, members.length]));
  const plannedCheckins = Object.values(groupPlans).reduce((sum, plan) => sum + plan.actions.filter((a) => a.type === 'checkin').length, 0);
  const plannedHelps = Object.values(groupPlans).reduce((sum, plan) => sum + plan.actions.filter((a) => a.type === 'help').length, 0);
  const scripted = scenario.scriptedDiscussion && scenario.scriptedDiscussion.messages
    ? scenario.scriptedDiscussion.messages.length
    : 0;
  const scriptedGroupActions = Object.values(groupPlans)
    .reduce((sum, plan) => sum + plan.actions.filter((action) => action.scripted).length, 0);
  const scriptedStates = scenario.scriptedDiscussion && scenario.scriptedDiscussion.messages
    ? scenario.scriptedDiscussion.messages.reduce((acc, message) => {
      const state = message.state || 'unspecified';
      acc[state] = (acc[state] || 0) + 1;
      return acc;
    }, {})
    : {};
  const expectedScriptedStates = expectedStatesForScenario(scenario);
  const plannedScriptedStates = plannedStatesForScenario(scenario);
  const expectedStrategyIds = expectedStrategyIdsForScenario(scenario);
  const stateScenarioCount = scenario.scriptedDiscussion && Array.isArray(scenario.scriptedDiscussion.scenarios)
    ? scenario.scriptedDiscussion.scenarios.length
    : 0;
  const stateScenarioIdentities = scenario.scriptedDiscussion && Array.isArray(scenario.scriptedDiscussion.scenarios)
    ? scenario.scriptedDiscussion.scenarios.map((item) => (
      (item.expectedOverlayTags || [])[0] || item.expectedProcessState || item.canonicalSubState
    ))
    : [];
  const enforceInterventionExpectations = Boolean(
    scenario.flow && scenario.flow.enforceScriptedInterventionExpectations
  );
  const expectedInterventionGates = scenario.scriptedDiscussion && scenario.scriptedDiscussion.messages
    ? scenario.scriptedDiscussion.messages.filter((item) => (
      enforceInterventionExpectations && item.waitForExpectedInterventionAfter
    )).length
    : 0;
  const expectedRestraintChecks = scenario.scriptedDiscussion && scenario.scriptedDiscussion.messages
    ? scenario.scriptedDiscussion.messages.filter((item) => (
      enforceInterventionExpectations && item.waitForNoInterventionAfter
    )).length
    : 0;
  const stateSuite = scenario.stateSuite || {};
  const strictCoverage = Boolean(
    scenario.strategyAudit && scenario.strategyAudit.enabled && scenario.strategyAudit.requireActualCoverage
  );
  const lastScriptedMessage = scenario.scriptedDiscussion && scenario.scriptedDiscussion.messages
    ? scenario.scriptedDiscussion.messages[scenario.scriptedDiscussion.messages.length - 1]
    : null;
  const lastScriptedMessageAtMs = lastScriptedMessage
    ? Number(lastScriptedMessage.offsetMs || 0)
    : 0;
  const scriptedDeadlineHeadroomMs = Math.max(
    0,
    Number(scenario.discussionDurationMs || 0) - lastScriptedMessageAtMs
  );

  console.log([
    `[plan] runId=${runId}`,
    `[plan] scenario=${scenario.name}`,
    `[plan] baseUrl=${scenario.baseUrl}`,
    `[plan] students=${scenario.totalStudents}`,
    `[plan] duration=${formatDuration(scenario.discussionDurationMs)}`,
    `[plan] rampUp=batchSize:${scenario.rampUp.batchSize}, interval:${formatDuration(scenario.rampUp.intervalMs)}`,
    `[plan] resourceMode=${scenario.resourceMode}`,
    `[plan] headless=${scenario.browser.headless}`,
    `[plan] fullFlow=${scenario.flow.fullFlow}`,
    `[plan] groupCounts=${JSON.stringify(groupCounts)}`,
    `[plan] plannedGroupCheckins=${plannedCheckins}`,
    `[plan] plannedGroupHelps=${plannedHelps}`,
    `[plan] scriptedGroupActions=${scriptedGroupActions}`,
    `[plan] scriptedMessages=${scripted}`,
    `[plan] scriptedLastMessageAt=${formatDuration(lastScriptedMessageAtMs)}`,
    `[plan] scriptedDeadlineHeadroom=${formatDuration(scriptedDeadlineHeadroomMs)}`,
    `[plan] scriptedStates=${JSON.stringify(scriptedStates)}`,
    `[plan] expectedScriptedStates=${JSON.stringify(expectedScriptedStates)}`,
    `[plan] plannedScriptedStates=${JSON.stringify(plannedScriptedStates)}`,
    `[plan] scriptedStateScenarios=${stateScenarioCount}`,
    `[plan] scriptedSceneStates=${JSON.stringify(stateScenarioIdentities)}`,
    `[plan] validationMode=${strictCoverage ? 'strict-server-coverage' : 'student-discussion-only'}`,
    `[plan] interventionAssertions=${enforceInterventionExpectations}`,
    `[plan] expectedInterventionGates=${expectedInterventionGates}`,
    `[plan] expectedRestraintChecks=${expectedRestraintChecks}`,
    `[plan] stateSuite=${stateSuite.mode || 'none'}`,
    `[plan] isolation=${stateSuite.isolation || 'not-configured'}`,
    `[plan] recoveryBridges=${Boolean(stateSuite.includeRecoveryBridges)}`,
    `[plan] expectedServerFallback=${stateSuite.requireModelFailureFallback ? 'unclassified' : 'none'}`,
    `[plan] expectedStrategyIds=${JSON.stringify(expectedStrategyIds)}`,
    `[plan] strategyAcceptance=${strictCoverage
      ? 'selected_strategy_id in strategy_route_manifest runtime route; exact primary is diagnostic'
      : 'teacher-controlled; not asserted by the student runner'}`,
    `[plan] expectedEmotionAgentEnabled=${strictCoverage
      ? Boolean(scenario.strategyAudit.expectedEmotionAgentEnabled)
      : 'teacher-controlled'}`,
    `[plan] captureStrategyAudit=${Boolean(scenario.strategyAudit && scenario.strategyAudit.enabled)}`,
    `[plan] captureConversation=${Boolean(scenario.flow.captureConversation)}`,
    `[plan] profileCounts=${JSON.stringify(counts)}`
  ].join('\n'));
}

function printSummary(summary, report) {
  const coverage = summary.scriptedStateCoverage;
  const scriptedStrategyCoverage = summary.scriptedStrategyCoverage;
  const actualStateCoverage = summary.actualStateCoverage;
  const actualStrategyCoverage = summary.actualStrategyCoverage;
  console.log([
    '',
    '[summary]',
    `runId: ${summary.runId}`,
    `validation mode: ${summary.validationMode}`,
    `audit available: ${summary.auditAvailable}`,
    `login: ${summary.counters.loginSuccess}/${summary.counters.loginAttempted}`,
    `discussionReady: ${summary.counters.discussionReady}/${summary.totalStudents}`,
    `pre questionnaires: ${summary.counters.preQuestionnaireSuccess}/${summary.counters.preQuestionnaireAttempted}`,
    `messages: ${summary.counters.messageSuccess}/${summary.counters.messageAttempted}`,
    `input integrity: ${summary.input_integrity.status} ${summary.input_integrity.successful_script_messages}/${summary.input_integrity.expected_script_messages}`,
    `message delivery attempts: ${summary.counters.messageDeliveryAttempted}, retries=${summary.counters.messageDeliveryRetried}, finalized=${summary.counters.messageDeliveryFinalized}`,
    `checkins: ${summary.counters.checkinSuccess}/${summary.counters.checkinAttempted}`,
    `helps: ${summary.counters.helpAccepted}/${summary.counters.helpAttempted}`,
    `ai input locks: observed=${summary.counters.aiInputLockObserved}, uiOk=${summary.counters.aiInputLockUiOk}, api423=${summary.counters.aiInputLockApiRejected}, violations=${summary.counters.aiInputLockViolation}, timeouts=${summary.counters.aiInputLockTimeouts}`,
    `lock observer: restored=${summary.counters.clientInputLockRestored}, races=${summary.counters.lockObserverRaces}, pipelineWaitP95=${summary.latencies.pipelineWaitMs.p95}, leaseP95=${summary.latencies.leaseDurationMs.p95}, clientBlockP95=${summary.latencies.continuousClientBlockMs.p95}`,
    `expected interventions: completed=${summary.counters.expectedInterventionCompleted}/${summary.counters.expectedInterventionStarted}, failed=${summary.counters.expectedInterventionFailed}`,
    `expected restraint: completed=${summary.counters.expectedNoInterventionCompleted}/${summary.counters.expectedNoInterventionStarted}, failed=${summary.counters.expectedNoInterventionFailed}`,
    `deliverables: ${summary.counters.deliverableSubmitted}/${summary.counters.deliverableAttempted}`,
    `post questionnaires: ${summary.counters.postQuestionnaireSuccess}/${summary.counters.postQuestionnaireAttempted}`,
    `message p95 ms: ${summary.latencies.messageMs.p95}`,
    `pageErrors: ${summary.counters.pageErrors}`,
    `consoleErrors: ${summary.counters.consoleErrors}`,
    `requestFailures: ${summary.counters.requestFailures}`,
    `studentFatalErrors: ${summary.counters.studentFatalErrors}`,
    `summary: ${report.summaryPath}`,
    `events: ${report.eventsPath}`,
    `errors: ${report.errorsPath}`
  ].join('\n'));
  if (summary.scriptedMessageProgress) {
    const progress = summary.scriptedMessageProgress;
    console.log(
      `scripted messages: successful=${progress.successful}/${progress.planned}, ` +
      `attempted=${progress.attempted}, failed=${progress.failed}, unsent=${progress.unsent}, ` +
      `complete=${progress.complete}`
    );
  }
  if (coverage) {
    const lines = [
      'scripted state labels are plan/send metrics only; they do not prove server detection',
      `scripted states successful: ${coverage.successful.length}/${coverage.expected.length}`,
      `scripted states missing: ${coverage.missingSuccessful.join(', ') || 'none'}`
    ];
    if (coverage.requiredAllFinalStates) {
      lines.push(`scripted states all-final-successful: ${coverage.allFinalStatesSuccessful}`);
    }
    console.log(lines.join('\n'));
  }
  if (actualStateCoverage) {
    console.log([
      `actual server states: ${actualStateCoverage.observedStates.length}/${actualStateCoverage.expectedStates.length}`,
      `actual server states missing: ${actualStateCoverage.missingStates.join(', ') || 'none'}`,
      `actual server state coverage passed: ${actualStateCoverage.passed}`
    ].join('\n'));
  }
  if (scriptedStrategyCoverage) {
    console.log([
      `scripted report scenarios: ${scriptedStrategyCoverage.scenarios.filter((item) => item.passed).length}/${scriptedStrategyCoverage.scenarios.length}`,
      `scripted strategies successful: ${scriptedStrategyCoverage.successfulStrategyIds.length}/${scriptedStrategyCoverage.expectedStrategyIds.length}`,
      `scripted strategies missing: ${scriptedStrategyCoverage.missingSuccessfulStrategyIds.join(', ') || 'none'}`
    ].join('\n'));
  }
  if (actualStrategyCoverage) {
    console.log([
      `actual server strategy routes: ${actualStrategyCoverage.scenarios.filter((item) => item.passed).length}/${actualStrategyCoverage.scenarios.length}`,
      `actual strategy IDs observed: ${actualStrategyCoverage.observedStrategyIds.join(', ') || 'none'}`,
      `strategy acceptance: ${actualStrategyCoverage.routeAcceptance}`,
      `context isolation passed: ${actualStrategyCoverage.contextIsolationPassed}`,
      `actual strategy coverage passed: ${actualStrategyCoverage.passed}`
    ].join('\n'));
  }
  const coverageStatistics = summary.coverage_statistics || summary.coverageStatistics;
  if (coverageStatistics) {
    const counts = coverageStatistics.counts || coverageStatistics;
    console.log([
      `actual published pipelines: ${counts.published_pipeline_count}`,
      `visible Agent messages: ${counts.visible_agent_message_count}`,
      `expected boundary completed: ${counts.expected_boundary_completed_count}`,
      `strict scenario coverage: ${counts.scenario_coverage_passed_count}/${counts.expected_intervention_cases}`,
      `inhibition cases passed: ${counts.inhibition_case_passed_count}/${coverageStatistics.inhibition_case_expected_count || 0}`
    ].join('\n'));
  }
  if (summary.modelFailureCoverage) {
    console.log(`model failure fallback passed: ${summary.modelFailureCoverage.passed}`);
  }
  if (summary.agentLockCoverage) {
    console.log(`agent lock recovery passed: ${summary.agentLockCoverage.passed}`);
  }
  if (summary.actualServerCoveragePassed !== null) {
    for (const name of [
      'planned_script_coverage',
      'message_send_coverage',
      'canonical_db_coverage',
      'teacher_api_coverage',
      'export_coverage',
      'intervention_coverage',
      'inhibition_coverage'
    ]) {
      const item = summary[name];
      console.log(`${name}: ${item ? item.passed : false}`);
    }
    console.log(`actual server coverage passed: ${summary.actualServerCoveragePassed}`);
  }
  if (report.transcriptJsonPath || report.transcriptMdPath) {
    console.log([
      `transcript json: ${report.transcriptJsonPath}`,
      `transcript md: ${report.transcriptMdPath}`,
      `strategy audit: ${report.strategyAuditPath || 'not captured'}`
    ].join('\n'));
  }
  if (report.p0Batch6Bundle) {
    console.log(`P0 batch 6 bundle: ${report.p0Batch6Bundle.bundleDir}`);
  }
}

function loadAuditor(scenario) {
  if (!(scenario.strategyAudit && scenario.strategyAudit.enabled)) return null;
  const filePath = path.resolve(scenario.studentsFile);
  if (filePath.toLowerCase().endsWith('.csv')) {
    const row = readCsvRecords(filePath).find(
      (item) => String(item.role || '').trim().toLowerCase() === 'teacher' && item.login_key
    );
    if (!row) return null;
    return {
      id: row.participant_code || row.username || row.key_name || 'strategy-auditor',
      loginKey: row.login_key,
      role: 'teacher'
    };
  }

  const raw = readJson(filePath);
  const candidates = [raw.auditor, ...(raw.teachers || [])].filter(Boolean);
  const item = candidates.find((candidate) => candidate.loginKey || candidate.login_key || candidate.key);
  if (!item) return null;
  return {
    id: item.id || item.username || 'strategy-auditor',
    loginKey: item.loginKey || item.login_key || item.key,
    role: 'teacher'
  };
}

function groupStudents(students) {
  return students.reduce((acc, student) => {
    const groupCode = student.group_code || student.groupCode || `G${String(student.group_no || 0).padStart(2, '0')}`;
    if (!acc[groupCode]) acc[groupCode] = [];
    acc[groupCode].push(student);
    return acc;
  }, {});
}

function attachScriptedDiscussion(scenario, students) {
  const script = scenario.scriptedDiscussion;
  if (!script || !Array.isArray(script.messages) || !script.messages.length) return;

  const templates = normalizeScriptMessages(script.messages);
  let messages = templates;

  if (script.repeatForEachGroup) {
    const groups = groupStudents(students);
    messages = [];
    for (const [groupCode, members] of Object.entries(groups)) {
      const byMember = new Map(members.map((student) => [Number(student.member_no || student.memberNo), student]));
      for (const template of templates) {
        const memberNo = scriptMessageMemberNo(template);
        const student = byMember.get(memberNo);
        messages.push({
          ...template,
          studentId: student ? student.id : `__missing_${groupCode}_M${memberNo}`,
          groupCode,
          group_no: student ? student.group_no : null,
          member_no: memberNo
        });
      }
    }
  } else {
    const groups = groupStudents(students);
    const byGroupAndMember = new Map();
    for (const [groupCode, members] of Object.entries(groups)) {
      for (const student of members) {
        byGroupAndMember.set(
          `${groupCode}:${Number(student.member_no || student.memberNo)}`,
          student
        );
      }
    }
    messages = templates.map((template) => {
      if (template.studentId) return template;
      const groupCode = String(template.groupCode || template.group_code || '');
      const memberNo = scriptMessageMemberNo(template);
      const student = byGroupAndMember.get(`${groupCode}:${memberNo}`);
      return {
        ...template,
        studentId: student ? student.id : `__missing_${groupCode}_M${memberNo}`,
        groupCode,
        group_no: student ? student.group_no : null,
        member_no: memberNo
      };
    });
  }

  messages = messages
    .map((message, index) => ({
      ...message,
      seq: index + 1,
      studentId: message.studentId || message.student || message.participantCode
    }))
    .sort((a, b) => (a.offsetMs - b.offsetMs) || String(a.studentId).localeCompare(String(b.studentId)));

  messages.forEach((message, index) => {
    message.seq = index + 1;
  });

  const byStudent = {};
  for (const student of students) {
    byStudent[student.id] = [];
  }
  for (const message of messages) {
    if (!byStudent[message.studentId]) byStudent[message.studentId] = [];
    byStudent[message.studentId].push(message);
  }
  for (const studentMessages of Object.values(byStudent)) {
    studentMessages.sort((a, b) => a.offsetMs - b.offsetMs);
  }

  scenario.scriptedDiscussion = {
    ...script,
    messages,
    byStudent,
    templateCount: templates.length
  };
}

function normalizeScriptMessages(rawMessages) {
  let elapsedMs = 0;
  return rawMessages.map((message, index) => {
    if (message.atSeconds !== undefined) {
      elapsedMs = Number(message.atSeconds) * 1000;
    } else {
      elapsedMs += Number(message.afterSeconds === undefined ? 0 : message.afterSeconds) * 1000;
    }
    return {
      ...message,
      seq: index + 1,
      studentId: message.studentId || message.student || message.participantCode,
      offsetMs: Math.max(0, Math.round(elapsedMs))
    };
  });
}

function scriptMessageMemberNo(message) {
  const explicit = message.memberNo === undefined ? message.member_no : message.memberNo;
  if (explicit !== undefined && explicit !== null && explicit !== '') {
    return Number(explicit);
  }
  const studentId = String(message.studentId || message.student || message.participantCode || '');
  const match = studentId.match(/-M(\d+)$/i);
  if (match) return Number(match[1]);
  throw new Error(`Scripted message ${message.seq || ''} is missing memberNo and cannot be repeated per group.`);
}

function randomRangeFromTuple(range) {
  const min = Number(range[0] || 0);
  const max = Number(range[1] === undefined ? min : range[1]);
  return randomInt(min, max);
}

function scheduleOffsets(count, durationMs, startRatio, endRatio, minGapMs) {
  if (!count) return [];
  const start = Math.floor(durationMs * startRatio);
  const end = Math.floor(durationMs * endRatio);
  if (count === 1) return [randomInt(start, end)];

  const span = Math.max(minGapMs * (count - 1), end - start);
  const step = Math.floor(span / count);
  const offsets = [];
  for (let i = 0; i < count; i += 1) {
    const base = Math.min(end, start + step * i + Math.floor(step / 2));
    const jitter = Math.floor(Math.min(step / 3, 45 * 1000));
    offsets.push(Math.max(start, Math.min(end, base + randomInt(-jitter, jitter))));
  }
  return offsets.sort((a, b) => a - b);
}

function scriptedGroupActionsForGroup(groupCode, scenario) {
  const rawActions = (scenario.flow && scenario.flow.scriptedGroupActions) || [];
  if (!rawActions.length) return [];

  return rawActions
    .filter((action) => !action.groupCode || action.groupCode === groupCode)
    .map((action, index) => ({
      id: action.id || `${groupCode}-scripted-${action.type}-${index + 1}`,
      type: action.type,
      checkinType: action.checkinType || action.checkin_type || 'event',
      offsetMs: scriptedActionOffsetMs(action),
      note: action.note || `Scripted ${action.type} action for ${groupCode}`,
      text: action.text,
      checkin: action.checkin || action.payload || null,
      scripted: true
    }));
}

function scriptedActionOffsetMs(action) {
  if (action.offsetMs !== undefined) return Number(action.offsetMs);
  if (action.atSeconds !== undefined) return Number(action.atSeconds) * 1000;
  if (action.afterSeconds !== undefined) return Number(action.afterSeconds) * 1000;
  return 0;
}

function normalizeNumber(value) {
  if (value === undefined || value === null || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function validateRunId(value) {
  const runId = String(value || '').trim();
  if (!/^[A-Za-z0-9._-]+$/.test(runId)) {
    throw new Error('--run-id may only contain letters, numbers, dots, underscores, and hyphens.');
  }
  return runId;
}

function formatServerCoverageGap(coverage) {
  if (!coverage) return 'audit unavailable';
  const gaps = [];
  if (!coverage.auditAvailable) {
    gaps.push('DB/API/export audit unavailable');
  }
  const namedCoverage = coverage.coverage || {};
  for (const [name, result] of Object.entries(namedCoverage)) {
    if (result && !result.passed) gaps.push(`${name}=failed`);
  }
  const stateCoverage = coverage.actualStateCoverage;
  if (stateCoverage && !stateCoverage.passed) {
    gaps.push(`missing states=${stateCoverage.missingStates.join('|') || 'scenario mismatch'}`);
  }
  const strategyCoverage = coverage.actualStrategyCoverage;
  if (strategyCoverage && !strategyCoverage.passed) {
    const failed = strategyCoverage.scenarios
      .filter((item) => !item.passed)
      .map((item) => item.scenarioId);
    gaps.push(`strategy routes=${failed.join('|') || 'context isolation'}`);
  }
  if (coverage.modelFailureCoverage && !coverage.modelFailureCoverage.passed) {
    gaps.push('model failure fallback not observed');
  }
  if (coverage.agentLockCoverage && !coverage.agentLockCoverage.passed) {
    gaps.push('agent lock did not reach a released terminal state');
  }
  return gaps.join('; ') || 'server audit incomplete';
}

function shuffle(items) {
  const copy = [...items];
  for (let i = copy.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [copy[i], copy[j]] = [copy[j], copy[i]];
  }
  return copy;
}

main().catch((error) => {
  console.error('[fatal]', process.env.DEBUG ? error.stack || error.message : error.message);
  process.exitCode = 1;
});
