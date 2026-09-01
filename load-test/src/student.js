const { createStudentContext } = require('./browser');
const {
  buildMessage,
  buildReplyLikeMessage,
  enterDiscussion,
  fetchGroupTranscript,
  login,
  maybeScroll,
  pageDiagnostic,
  randomThinkTime,
  requestStudentHelp,
  sendDiscussionMessage,
  submitCheckin,
  submitGroupDeliverable,
  submitPostEmotionCheckin,
  submitQuestionnaires,
  shouldReply,
  shouldSendMessage,
  waitForDiscussionReady,
  waitForExpectedIntervention,
  verifyNoIntervention
} = require('./actions');
const { buildClientMessageId } = require('./messageDelivery');
const { sleep } = require('./utils');

async function runStudent({ browser, student, scenario, metrics, runState }) {
  let context = null;
  let page = null;
  metrics.registerStudent(student);

  try {
    metrics.recordStudentPhase(student.id, 'login_start');
    metrics.recordLoginAttempt(student.id);
    try {
      const opened = await openLoggedInStudentPage({
        browser,
        student,
        scenario,
        metrics
      });
      context = opened.context;
      page = opened.page;
      const loginResult = opened.loginResult;
      metrics.recordLoginSuccess(student.id, loginResult.latencyMs);
      metrics.recordStudentPhase(student.id, 'login_success', {
        url: loginResult.url,
        contextRecoveryCount: opened.contextRecoveryCount
      });
    } catch (error) {
      metrics.recordLoginFailure(student.id, error);
      throw error;
    }

    if (scenario.flow.submitPreQuestionnaires) {
      metrics.recordStudentPhase(student.id, 'pre_questionnaires_start');
      await submitQuestionnaires(page, scenario, student, 'pre', metrics);
      metrics.recordStudentPhase(student.id, 'pre_questionnaires_done');
    }

    metrics.recordStudentPhase(student.id, 'enter_discussion_start');
    const entered = await enterDiscussion(page, scenario);
    metrics.recordDiscussionEntered(student.id, entered);
    metrics.recordStudentPhase(student.id, 'enter_discussion_done', entered);
    runState.markEntered(student.id);

    metrics.recordStudentPhase(student.id, 'waiting_for_discussion_window');
    await runState.waitUntilStarted();
    if (runState.shouldStop()) return;

    try {
      metrics.recordStudentPhase(student.id, 'discussion_ready_start');
      const ready = await waitForDiscussionReady(page, scenario);
      metrics.recordDiscussionReady(student.id, ready);
      metrics.recordStudentPhase(student.id, 'discussion_ready_done', ready);
    } catch (error) {
      const diagnostic = await pageDiagnostic(page);
      metrics.recordDiscussionReadyFailure(student.id, error, { diagnostic });
      throw error;
    }

    if (scenario.scriptedDiscussion && scenario.scriptedDiscussion.byStudent) {
      await runScriptedDiscussion({ page, student, scenario, metrics, runState });
    } else {
      await runRandomDiscussion({ page, student, scenario, metrics, runState });
    }

    if (runState.stopRequested) return;

    if (scenario.flow.submitDeliverable) {
      await finishGroupDeliverable({ page, student, scenario, metrics, runState });
    }

    if (scenario.flow.submitPostCheckin) {
      metrics.recordCheckinAttempt(student.id, student.group_code, 'post');
      try {
        await submitPostEmotionCheckin(page, scenario, student);
        metrics.recordCheckinSuccess(student.id, student.group_code, 'post');
      } catch (error) {
        metrics.recordCheckinFailure(student.id, student.group_code, 'post', error);
        throw error;
      }
    }

    if (scenario.flow.submitPostQuestionnaires) {
      await submitQuestionnaires(page, scenario, student, 'post', metrics);
    }

    await captureGroupTranscriptForAudit({ page, student, scenario, metrics, runState });
  } catch (error) {
    metrics.recordStudentFatalError(student.id, error);
  } finally {
    const abandoned = runState.abandonScriptedMessagesForStudent(student.id);
    if (abandoned.length) {
      metrics.event('scripted_messages_abandoned', {
        studentId: student.id,
        abandoned_count: abandoned.length,
        group_codes: [...new Set(abandoned.map((item) => item.groupCode))],
        sequences: abandoned.slice(0, 24).map((item) => item.sequence),
        sequences_truncated: abandoned.length > 24
      });
    }
    if (context) {
      await context.close().catch(() => null);
    }
  }
}

async function openLoggedInStudentPage({
  browser,
  student,
  scenario,
  metrics,
  contextFactory = createStudentContext,
  loginFn = login,
  bindMetrics = bindPageMetrics
}) {
  const maxContextAttempts = Math.max(
    1,
    Number((scenario.flow && scenario.flow.loginContextAttempts) || 2)
  );
  let lastError = null;

  for (let contextAttempt = 1; contextAttempt <= maxContextAttempts; contextAttempt += 1) {
    let context = null;
    try {
      context = await contextFactory(browser, scenario, student);
      const page = await context.newPage();
      bindMetrics(page, student, metrics);
      const loginResult = await loginFn(page, student, scenario);
      return {
        context,
        page,
        loginResult,
        contextRecoveryCount: contextAttempt - 1
      };
    } catch (error) {
      lastError = error;
      if (context) await context.close().catch(() => null);
      const recoverable = isBrowserPageCrash(error);
      if (!recoverable || contextAttempt >= maxContextAttempts) throw error;
      if (metrics && typeof metrics.event === 'function') {
        metrics.event('login_context_recreated', {
          studentId: student.id,
          context_attempt: contextAttempt,
          reason: 'browser_page_unusable',
          error: String(error && error.message || error).slice(0, 240)
        });
      }
    }
  }

  throw lastError || new Error(`unable to create a login page for ${student.id}`);
}

function isBrowserPageCrash(error) {
  return /page crash|target page, context or browser has been closed|page\.goto:.*closed|browser has been closed/i.test(
    String(error && error.message || error || '')
  );
}

async function captureGroupTranscriptForAudit({ page, student, scenario, metrics, runState }) {
  if (!scenario.flow.captureConversation) return;

  const groupCode = student.group_code || student.groupCode;
  if (!await runState.waitForScriptedMessagesComplete(groupCode)) return;
  if (!groupCode || !runState.claimGroupTranscript(groupCode, student.id)) return;

  metrics.recordTranscriptAttempt(student.id, groupCode);
  try {
    const transcript = await fetchGroupTranscript(page, scenario, student);
    metrics.recordTranscriptCaptured(transcript);
    runState.markGroupTranscriptCaptured(groupCode, {
      studentId: student.id,
      messageCount: transcript.messages.length
    });
  } catch (error) {
    metrics.recordTranscriptFailure(student.id, groupCode, error);
    runState.releaseGroupTranscript(groupCode, student.id);
  }
}

async function runRandomDiscussion({ page, student, scenario, metrics, runState }) {
  let seq = 0;
  let nextMessageAt = Date.now() + randomThinkTime(student.profile);
  while (!runState.shouldStop()) {
    await handleDueGroupActions({ page, student, scenario, metrics, runState });

    if (Date.now() >= nextMessageAt) {
      if (await maybeScroll(page, scenario, student.profile)) {
        metrics.recordScroll(student.id);
      }

      if (shouldSendMessage(student.profile)) {
        seq += 1;
        await sendOneMessage({
          page,
          student,
          scenario,
          metrics,
          runState,
          seq,
          kind: 'message'
        });
      }

      if (!runState.shouldStop() && shouldReply(student.profile)) {
        seq += 1;
        await sendOneMessage({
          page,
          student,
          scenario,
          metrics,
          runState,
          seq,
          kind: 'reply'
        });
      }

      nextMessageAt = Date.now() + randomThinkTime(student.profile);
    }

    await sleep(Math.max(500, scenario.flow.actionTickMs || 5000));
  }
}

async function runScriptedDiscussion({ page, student, scenario, metrics, runState }) {
  const script = scenario.scriptedDiscussion || {};
  const messages = script.byStudent[student.id] || [];
  const tickMs = Math.max(500, scenario.flow.actionTickMs || 5000);

  for (const message of messages) {
    const scheduledAt = runState.startedAt + message.offsetMs;
    while (canRunScheduledScriptedMessage(runState, scheduledAt) && Date.now() < scheduledAt) {
      await handleDueGroupActions({ page, student, scenario, metrics, runState });
      await sleep(Math.min(tickMs, Math.max(500, scheduledAt - Date.now())));
    }
    if (!canRunScheduledScriptedMessage(runState, scheduledAt)) return;

    if (await maybeScroll(page, scenario, student.profile || { scrollChance: 0 })) {
      metrics.recordScroll(student.id);
    }

    await sendScriptedMessage({
      page,
      student,
      scenario,
      metrics,
      runState,
      scriptMessage: message
    });
  }

  while (!runState.shouldStop()) {
    await handleDueGroupActions({ page, student, scenario, metrics, runState });
    await sleep(tickMs);
  }
}

function canRunScheduledScriptedMessage(runState, scheduledAt) {
  if (!runState || runState.stopRequested) return false;
  if (runState.endAt && scheduledAt > runState.endAt) return false;
  // A prior scripted send can be held by the expected AI input lock until after
  // the nominal discussion end. Messages that were scheduled inside the window
  // must still drain so deterministic coverage does not silently lose them.
  return true;
}

async function handleDueGroupActions({ page, student, scenario, metrics, runState }) {
  const groupCode = student.group_code || student.groupCode;
  if (!groupCode) return;
  const actions = runState.claimDueGroupActions(groupCode, student.id);
  for (const action of actions) {
    if (action.type === 'checkin') {
      metrics.recordCheckinAttempt(student.id, groupCode, action.checkinType);
      try {
        await submitCheckin(
          page,
          scenario,
          student,
          action.checkinType,
          action.note,
          action.checkin || action.payload || {}
        );
        metrics.recordCheckinSuccess(student.id, groupCode, action.checkinType);
        runState.markGroupActionCompleted(groupCode, action.id, { studentId: student.id });
      } catch (error) {
        metrics.recordCheckinFailure(student.id, groupCode, action.checkinType, error);
        runState.markGroupActionFailed(groupCode, action.id, error);
      }
      continue;
    }

    if (action.type === 'help') {
      metrics.recordHelpAttempt(student.id, groupCode);
      try {
        const result = await requestStudentHelp(page, scenario, student, action.text);
        metrics.recordHelpAccepted(student.id, groupCode, result);
        runState.markGroupActionCompleted(groupCode, action.id, { studentId: student.id, status: result.status });
      } catch (error) {
        metrics.recordHelpFailure(student.id, groupCode, error);
        runState.markGroupActionFailed(groupCode, action.id, error);
      }
    }
  }
}

async function sendScriptedMessage({ page, student, scenario, metrics, runState, scriptMessage }) {
  const state = scriptMessage.state || 'script';
  const marker = `[LOAD_SCRIPT:${runState.runId}:${student.id}:${scriptMessage.seq}:${state}]`;
  const content = scriptMessage.text || '';
  const groupCode = student.group_code || student.groupCode;
  const kind = `script:${state}`;
  const turnGranted = await runState.waitForScriptedMessageTurn(groupCode, scriptMessage.seq);
  if (!turnGranted) return;
  const cadenceGranted = await runState.waitForScriptedMessageCadence(
    groupCode,
    scriptMessage.minimumActualGapBeforeSeconds
  );
  if (!cadenceGranted) return;
  const spacingGranted = await runState.waitForPreviousInterventionGap(
    groupCode,
    scriptMessage.minimumPreviousInterventionGapSeconds
  );
  if (!spacingGranted) return;
  const message = {
    marker,
    content,
    visibleText: content,
    messageIndex: scriptMessage.seq,
    clientMessageId: buildClientMessageId({
      runId: runState.runId,
      studentId: student.id,
      messageIndex: scriptMessage.seq,
      kind
    })
  };

  const scriptDetails = {
    scenarioId: scriptMessage.scenarioId || null,
    phase: scriptMessage.phase || 'scenario',
    plannedState: state
  };
  metrics.recordMessageAttempt(student.id, scriptMessage.seq, marker, kind, {
    ...scriptDetails,
    messageIndex: scriptMessage.seq,
    clientMessageId: message.clientMessageId
  });
  const startedAt = Date.now();
  let result = null;

  try {
    result = await sendDiscussionMessage(page, scenario, message, {
      metrics,
      studentId: student.id,
      groupCode,
      scenarioId: scriptMessage.scenarioId || null
    });
    metrics.recordMessageSuccess({
      studentId: student.id,
      seq: scriptMessage.seq,
      marker,
      latencyMs: Date.now() - startedAt,
      messageId: result.messageId,
      kind,
      messageIndex: scriptMessage.seq,
      clientMessageId: message.clientMessageId,
      finalStatus: result.delivery && result.delivery.final_status,
      deliveryAttemptCount: result.delivery && result.delivery.attempt_count,
      ...scriptDetails
    });
    const gateObserver = { metrics, studentId: student.id, groupCode };
    const enforceInterventionExpectations = Boolean(
      scenario.flow && scenario.flow.enforceScriptedInterventionExpectations
    );
    if (enforceInterventionExpectations && scriptMessage.waitForExpectedInterventionAfter) {
      const intervention = await waitForExpectedIntervention(page, scenario, gateObserver, {
        afterMessageId: result.messageId,
        scenarioId: scriptMessage.scenarioId,
        minimumPauseSeconds: scriptMessage.minimumPauseSeconds
      });
      if (intervention.terminalReason === 'PUBLISHED' && intervention.agentPublishedAtMs) {
        runState.recordObservedIntervention(groupCode, intervention.agentPublishedAtMs);
      }
    }
    if (enforceInterventionExpectations && scriptMessage.waitForNoInterventionAfter) {
      await verifyNoIntervention(page, scenario, gateObserver, {
        afterMessageId: result.messageId,
        scenarioId: scriptMessage.scenarioId,
        observationSeconds: scriptMessage.noInterventionObservationSeconds
      });
    }
  } catch (error) {
    if (!result) {
      metrics.recordMessageFailure(
        student.id,
        scriptMessage.seq,
        marker,
        error,
        kind,
        {
          ...scriptDetails,
          messageIndex: scriptMessage.seq,
          clientMessageId: message.clientMessageId,
          finalStatus: error.deliveryStatus,
          delivery: error.delivery
        }
      );
      runState.requestStop(
        `INPUT_INCOMPLETE: scripted message ${scriptMessage.seq} failed ` +
        `with ${error.deliveryStatus || 'UNKNOWN'}`
      );
    } else if (scenario.flow && scenario.flow.continueAfterScriptBoundaryFailure) {
      metrics.error('script_boundary_nonfatal', student.id, error, {
        groupCode,
        scenarioId: scriptMessage.scenarioId || null,
        messageId: result.messageId
      });
    } else {
      runState.requestStop(
        `script boundary failed for ${scriptMessage.scenarioId || scriptMessage.seq}: ${error.message}`
      );
      throw error;
    }
  } finally {
    runState.completeScriptedMessageTurn(groupCode, scriptMessage.seq);
  }
}

async function finishGroupDeliverable({ page, student, scenario, metrics, runState }) {
  const groupCode = student.group_code || student.groupCode;
  if (!groupCode) return;

  if (runState.claimGroupSubmission(groupCode, student.id)) {
    metrics.recordDeliverableAttempt(student.id, groupCode);
    try {
      const result = await submitGroupDeliverable(page, scenario, student, runState.runId);
      metrics.recordDeliverableSubmitted(student.id, groupCode, result);
      runState.markGroupSubmitted(groupCode, result);
    } catch (error) {
      metrics.recordDeliverableFailure(student.id, groupCode, error);
      runState.releaseGroupSubmission(groupCode, student.id);
    }
  }

  const submitted = await runState.waitForGroupSubmitted(groupCode, scenario.timeouts.groupSubmitWaitMs);
  if (!submitted && runState.claimGroupSubmission(groupCode, student.id)) {
    metrics.recordDeliverableAttempt(student.id, groupCode);
    try {
      const result = await submitGroupDeliverable(page, scenario, student, runState.runId);
      metrics.recordDeliverableSubmitted(student.id, groupCode, result);
      runState.markGroupSubmitted(groupCode, result);
      return;
    } catch (error) {
      metrics.recordDeliverableFailure(student.id, groupCode, error);
      runState.releaseGroupSubmission(groupCode, student.id);
      throw error;
    }
  }

  if (!runState.isGroupSubmitted(groupCode)) {
    throw new Error(`group ${groupCode} deliverable was not submitted`);
  }
}

async function sendOneMessage({ page, student, scenario, metrics, runState, seq, kind }) {
  let message = buildMessage({
    runId: runState.runId,
    studentId: student.id,
    seq,
    scenario,
    kind
  });

  if (kind === 'reply') {
    message = await buildReplyLikeMessage(page, scenario, message);
  }
  message.messageIndex = seq;
  message.clientMessageId = buildClientMessageId({
    runId: runState.runId,
    studentId: student.id,
    messageIndex: seq,
    kind
  });

  metrics.recordMessageAttempt(student.id, seq, message.marker, kind, {
    messageIndex: seq,
    clientMessageId: message.clientMessageId
  });
  const startedAt = Date.now();

  try {
    const result = await sendDiscussionMessage(page, scenario, message, {
      metrics,
      studentId: student.id,
      groupCode: student.group_code || student.groupCode
    });
    metrics.recordMessageSuccess({
      studentId: student.id,
      seq,
      marker: message.marker,
      latencyMs: Date.now() - startedAt,
      messageId: result.messageId,
      kind,
      messageIndex: seq,
      clientMessageId: message.clientMessageId,
      finalStatus: result.delivery && result.delivery.final_status,
      deliveryAttemptCount: result.delivery && result.delivery.attempt_count
    });
  } catch (error) {
    metrics.recordMessageFailure(student.id, seq, message.marker, error, kind, {
      messageIndex: seq,
      clientMessageId: message.clientMessageId,
      finalStatus: error.deliveryStatus,
      delivery: error.delivery
    });
  }
}

function bindPageMetrics(page, student, metrics) {
  page.on('pageerror', (error) => {
    metrics.recordPageError(student.id, error);
  });

  page.on('console', (message) => {
    if (message.type() === 'error') {
      metrics.recordConsoleError(student.id, message.text());
    }
  });

  page.on('requestfailed', (request) => {
    const type = request.resourceType();
    if (['image', 'media', 'font'].includes(type)) return;
    metrics.recordRequestFailure(student.id, request.url(), request.failure());
  });

  page.on('response', (response) => {
    const status = response.status();
    if (status >= 500 || (status >= 400 && response.url().includes('/api/'))) {
      metrics.recordHttpError(student.id, response.url(), status);
    }
  });

  page.on('websocket', (ws) => {
    ws.on('close', () => {
      metrics.recordWebsocketClosed(student.id, ws.url());
    });
    ws.on('socketerror', (error) => {
      metrics.recordWebsocketError(student.id, ws.url(), error);
    });
  });
}

module.exports = {
  canRunScheduledScriptedMessage,
  isBrowserPageCrash,
  openLoggedInStudentPage,
  runStudent
};
