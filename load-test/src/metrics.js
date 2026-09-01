const fs = require('fs');
const path = require('path');
const { buildScriptedStateCoverage } = require('./stateCoverage');
const {
  buildActualStrategyCoverage,
  buildScriptedStrategyCoverage
} = require('./strategyCoverage');
const {
  buildActualServerCoverage,
  formatCoverageStatisticsMarkdown
} = require('./serverCoverage');
const {
  buildP0Batch6Acceptance,
  writeP0Batch6Bundle
} = require('./p0Batch6Audit');
const { avg, csvEscape, ensureDir, percentile, writeJson } = require('./utils');

class Metrics {
  constructor({ runId, scenario }) {
    this.runId = runId;
    this.scenario = scenario;
    this.startedAt = new Date();
    this.endedAt = null;
    this.events = [];
    this.errors = [];
    this.transcripts = [];
    this.strategyAuditSnapshots = {};
    this.students = new Map();
    this.latencies = {
      loginMs: [],
      messageMs: [],
      aiInputLockWaitMs: [],
      pipelineWaitMs: [],
      leaseDurationMs: [],
      continuousClientBlockMs: []
    };
    this.clientLockTrackers = new Map();
    this.counters = {
      loginAttempted: 0,
      loginSuccess: 0,
      loginFailed: 0,
      discussionEntered: 0,
      discussionReady: 0,
      discussionReadyFailed: 0,
      preQuestionnaireAttempted: 0,
      preQuestionnaireSuccess: 0,
      preQuestionnaireFailed: 0,
      postQuestionnaireAttempted: 0,
      postQuestionnaireSuccess: 0,
      postQuestionnaireFailed: 0,
      checkinAttempted: 0,
      checkinSuccess: 0,
      checkinFailed: 0,
      helpAttempted: 0,
      helpAccepted: 0,
      helpFailed: 0,
      roomAiIntervening: 0,
      aiInputLockObserved: 0,
      aiInputLockUiOk: 0,
      aiInputLockApiRejected: 0,
      aiInputLockViolation: 0,
      aiInputLockWaits: 0,
      aiInputLockTimeouts: 0,
      clientInputLockRestored: 0,
      lockObserverRaces: 0,
      expectedInterventionStarted: 0,
      expectedInterventionCompleted: 0,
      expectedInterventionFailed: 0,
      expectedNoInterventionStarted: 0,
      expectedNoInterventionCompleted: 0,
      expectedNoInterventionFailed: 0,
      deliverableAttempted: 0,
      deliverableSubmitted: 0,
      deliverableFailed: 0,
      transcriptCaptureAttempted: 0,
      transcriptCaptureSuccess: 0,
      transcriptCaptureFailed: 0,
      messageAttempted: 0,
      messageSuccess: 0,
      messageFailed: 0,
      messageVisibleToSender: 0,
      messageDeliveryAttempted: 0,
      messageDeliveryRetried: 0,
      messageDeliveryFinalized: 0,
      inputIncomplete: 0,
      scrollActions: 0,
      pageErrors: 0,
      consoleErrors: 0,
      requestFailures: 0,
      httpErrors: 0,
      websocketClosed: 0,
      websocketErrors: 0,
      studentFatalErrors: 0
    };
  }

  ensureStudent(studentId) {
    if (!this.students.has(studentId)) {
      this.students.set(studentId, {
        studentId,
        profile: null,
        lastPhase: 'registered',
        loginSuccess: false,
        enteredDiscussion: false,
        discussionReady: false,
        preQuestionnairesSubmitted: 0,
        postQuestionnairesSubmitted: 0,
        checkinsSubmitted: 0,
        helpsAccepted: 0,
        deliverableSubmitted: false,
        messagesAttempted: 0,
        messagesSuccess: 0,
        messagesFailed: 0,
        pageErrors: 0,
        consoleErrors: 0,
        requestFailures: 0,
        fatalError: null
      });
    }
    return this.students.get(studentId);
  }

  recordStudentPhase(studentId, phase, details = {}) {
    const state = this.ensureStudent(studentId);
    state.lastPhase = phase;
    this.event('student_phase', { studentId, phase, ...details });
  }

  event(type, data = {}) {
    this.events.push({
      ts: new Date().toISOString(),
      type,
      ...data
    });
  }

  error(type, studentId, error, extra = {}) {
    const message = normalizeError(error);
    const record = {
      ts: new Date().toISOString(),
      type,
      studentId,
      error: message,
      ...extra
    };
    this.errors.push(record);
    this.event(type, record);
  }

  registerStudent(student) {
    const state = this.ensureStudent(student.id);
    state.profile = student.profileName || null;
  }

  recordLoginAttempt(studentId) {
    this.ensureStudent(studentId);
    this.counters.loginAttempted += 1;
    this.event('login_attempt', { studentId });
  }

  recordLoginSuccess(studentId, latencyMs) {
    const state = this.ensureStudent(studentId);
    state.loginSuccess = true;
    this.counters.loginSuccess += 1;
    this.latencies.loginMs.push(latencyMs);
    this.event('login_success', { studentId, latencyMs });
  }

  recordLoginFailure(studentId, error) {
    this.ensureStudent(studentId);
    this.counters.loginFailed += 1;
    this.error('login_failure', studentId, error);
  }

  recordDiscussionEntered(studentId, details = {}) {
    const state = this.ensureStudent(studentId);
    if (!state.enteredDiscussion) this.counters.discussionEntered += 1;
    state.enteredDiscussion = true;
    this.event('discussion_entered', { studentId, ...details });
  }

  recordDiscussionReady(studentId, details = {}) {
    const state = this.ensureStudent(studentId);
    if (!state.discussionReady) this.counters.discussionReady += 1;
    state.discussionReady = true;
    this.event('discussion_ready', { studentId, ...details });
  }

  recordDiscussionReadyFailure(studentId, error, details = {}) {
    this.counters.discussionReadyFailed += 1;
    this.error('discussion_ready_failure', studentId, error, details);
  }

  recordQuestionnaireAttempt(studentId, stage, questionnaireId) {
    this.ensureStudent(studentId);
    const prefix = stage === 'post' ? 'post' : 'pre';
    this.counters[`${prefix}QuestionnaireAttempted`] += 1;
    this.event('questionnaire_attempt', { studentId, stage, questionnaireId });
  }

  recordQuestionnaireSuccess(studentId, stage, questionnaireId) {
    const state = this.ensureStudent(studentId);
    const prefix = stage === 'post' ? 'post' : 'pre';
    this.counters[`${prefix}QuestionnaireSuccess`] += 1;
    if (stage === 'post') state.postQuestionnairesSubmitted += 1;
    else state.preQuestionnairesSubmitted += 1;
    this.event('questionnaire_success', { studentId, stage, questionnaireId });
  }

  recordQuestionnaireFailure(studentId, stage, questionnaireId, error) {
    this.ensureStudent(studentId);
    const prefix = stage === 'post' ? 'post' : 'pre';
    this.counters[`${prefix}QuestionnaireFailed`] += 1;
    this.error('questionnaire_failure', studentId, error, { stage, questionnaireId });
  }

  recordCheckinAttempt(studentId, groupCode, checkinType) {
    this.ensureStudent(studentId);
    this.counters.checkinAttempted += 1;
    this.event('checkin_attempt', { studentId, groupCode, checkinType });
  }

  recordCheckinSuccess(studentId, groupCode, checkinType) {
    const state = this.ensureStudent(studentId);
    state.checkinsSubmitted += 1;
    this.counters.checkinSuccess += 1;
    this.event('checkin_success', { studentId, groupCode, checkinType });
  }

  recordCheckinFailure(studentId, groupCode, checkinType, error) {
    this.ensureStudent(studentId);
    this.counters.checkinFailed += 1;
    this.error('checkin_failure', studentId, error, { groupCode, checkinType });
  }

  recordHelpAttempt(studentId, groupCode) {
    this.ensureStudent(studentId);
    this.counters.helpAttempted += 1;
    this.event('help_attempt', { studentId, groupCode });
  }

  recordHelpAccepted(studentId, groupCode, result = {}) {
    const state = this.ensureStudent(studentId);
    state.helpsAccepted += 1;
    this.counters.helpAccepted += 1;
    this.event('help_accepted', { studentId, groupCode, status: result.status });
  }

  recordHelpFailure(studentId, groupCode, error, result = {}) {
    this.ensureStudent(studentId);
    this.counters.helpFailed += 1;
    if (isRoomAiInterveningError(error, result)) {
      this.counters.roomAiIntervening += 1;
      this.event('room_ai_intervening', {
        studentId,
        groupCode,
        status: result.status || result.statusCode || 423,
        error: normalizeError(error)
      });
    }
    this.error('help_failure', studentId, error, { groupCode, status: result.status });
  }

  recordAiInputLockObserved(data = {}) {
    this.counters.aiInputLockObserved += 1;
    const observedAt = normalizeMetricTimestamp(data.observed_at || data.observedAt);
    const key = lockTrackerKey(data);
    const tracker = this.clientLockTrackers.get(key) || {
      active: false,
      startedAt: null,
      ownerIds: new Set()
    };
    if (!tracker.active) {
      tracker.active = true;
      tracker.startedAt = observedAt;
      tracker.ownerIds = new Set();
    }
    const ownerId = data.lock_owner_pipeline_run_id ||
      data.lockOwnerPipelineRunId ||
      data.activeInterventionRunId ||
      data.active_intervention_run_id;
    if (ownerId !== null && ownerId !== undefined && String(ownerId) !== '') {
      tracker.ownerIds.add(String(ownerId));
    }
    this.clientLockTrackers.set(key, tracker);
    this.event('ai_input_lock_observed', {
      ...data,
      observed_at: observedAt,
      client_block_started_at: tracker.startedAt,
      sequential_lease_count: Math.max(1, tracker.ownerIds.size)
    });
  }

  recordAiInputLockUiOk(data = {}) {
    this.counters.aiInputLockUiOk += 1;
    this.event('ai_input_lock_ui_ok', data);
  }

  recordAiInputLockApiRejected(data = {}) {
    this.counters.aiInputLockApiRejected += 1;
    this.event('ai_input_lock_api_rejected', data);
  }

  recordAiInputLockViolation(data = {}) {
    this.counters.aiInputLockViolation += 1;
    this.event('ai_input_lock_violation', data);
  }

  recordAiInputLockWait(data = {}) {
    const waitMs = Number(data.waitMs || 0);
    this.counters.aiInputLockWaits += 1;
    if (Number.isFinite(waitMs) && waitMs >= 0) this.latencies.aiInputLockWaitMs.push(waitMs);
    this.event('ai_input_lock_wait', data);
  }

  recordAiInputLockTimeout(data = {}) {
    const waitMs = Number(data.waitMs || 0);
    this.counters.aiInputLockTimeouts += 1;
    if (Number.isFinite(waitMs) && waitMs >= 0) this.latencies.aiInputLockWaitMs.push(waitMs);
    this.event('ai_input_lock_timeout', data);
  }

  recordAiInputLockRestored(data = {}) {
    const key = lockTrackerKey(data);
    const tracker = this.clientLockTrackers.get(key);
    const restoredAt = normalizeMetricTimestamp(
      data.client_input_restored_at || data.clientInputRestoredAt
    );
    if (!tracker || !tracker.active) return;
    const duration = finiteNonNegative(data.continuous_block_duration) ?? durationBetween(
      tracker.startedAt,
      restoredAt
    );
    this.counters.clientInputLockRestored += 1;
    if (duration !== null) this.latencies.continuousClientBlockMs.push(duration);
    this.event('client_input_restored', {
      ...data,
      client_block_started_at: tracker.startedAt,
      client_input_restored_at: restoredAt,
      continuous_block_duration: duration,
      sequential_lease_count: Math.max(1, tracker.ownerIds.size)
    });
    this.clientLockTrackers.delete(key);
  }

  recordLockObserverRace(data = {}) {
    this.counters.lockObserverRaces += 1;
    this.event('lock_observer_race', {
      reason: 'LOCK_OBSERVER_RACE',
      ...data
    });
  }

  recordAiLockObserverMismatch(data = {}) {
    this.event('ai_lock_observer_mismatch', data);
  }

  recordExpectedInterventionStarted(data = {}) {
    this.counters.expectedInterventionStarted += 1;
    this.event('expected_intervention_started', data);
  }

  recordExpectedInterventionCompleted(data = {}) {
    this.counters.expectedInterventionCompleted += 1;
    const pipelineWaitMs = finiteNonNegative(data.pipeline_wait_duration);
    const leaseDurationMs = finiteNonNegative(data.lease_duration);
    if (pipelineWaitMs !== null) this.latencies.pipelineWaitMs.push(pipelineWaitMs);
    if (leaseDurationMs !== null) this.latencies.leaseDurationMs.push(leaseDurationMs);
    this.event('expected_intervention_completed', data);
  }

  recordExpectedInterventionFailed(data = {}) {
    this.counters.expectedInterventionFailed += 1;
    this.event('expected_intervention_failed', data);
  }

  recordExpectedNoInterventionStarted(data = {}) {
    this.counters.expectedNoInterventionStarted += 1;
    this.event('expected_no_intervention_started', data);
  }

  recordExpectedNoInterventionCompleted(data = {}) {
    this.counters.expectedNoInterventionCompleted += 1;
    this.event('expected_no_intervention_completed', data);
  }

  recordExpectedNoInterventionFailed(data = {}) {
    this.counters.expectedNoInterventionFailed += 1;
    this.event('expected_no_intervention_failed', data);
  }

  recordDeliverableAttempt(studentId, groupCode) {
    this.ensureStudent(studentId);
    this.counters.deliverableAttempted += 1;
    this.event('deliverable_attempt', { studentId, groupCode });
  }

  recordDeliverableSubmitted(studentId, groupCode, result = {}) {
    const state = this.ensureStudent(studentId);
    state.deliverableSubmitted = true;
    this.counters.deliverableSubmitted += 1;
    this.event('deliverable_submitted', { studentId, groupCode, ...result });
  }

  recordDeliverableFailure(studentId, groupCode, error) {
    this.ensureStudent(studentId);
    this.counters.deliverableFailed += 1;
    this.error('deliverable_failure', studentId, error, { groupCode });
  }

  recordTranscriptAttempt(studentId, groupCode) {
    this.ensureStudent(studentId);
    this.counters.transcriptCaptureAttempted += 1;
    this.event('transcript_capture_attempt', { studentId, groupCode });
  }

  recordTranscriptCaptured(transcript) {
    const normalized = normalizeTranscript(transcript, this.runId, this.scenario);
    this.transcripts.push(normalized);
    this.counters.transcriptCaptureSuccess += 1;
    this.event('transcript_captured', {
      studentId: normalized.capturedBy,
      groupCode: normalized.groupCode,
      messageCount: normalized.messageCount,
      agentMessageCount: normalized.agentMessageCount
    });
  }

  recordTranscriptFailure(studentId, groupCode, error) {
    this.ensureStudent(studentId);
    this.counters.transcriptCaptureFailed += 1;
    this.error('transcript_capture_failure', studentId, error, { groupCode });
  }

  recordStrategyAuditSnapshot(phase, snapshot) {
    this.strategyAuditSnapshots[phase] = snapshot;
    this.event('strategy_audit_snapshot', {
      phase,
      sessionId: snapshot && snapshot.sessionId,
      groupCount: snapshot && snapshot.groups ? snapshot.groups.length : 0
    });
  }

  actualStrategyCoverage() {
    return buildActualStrategyCoverage(
      this.scenario,
      this.strategyAuditSnapshots.baseline,
      this.strategyAuditSnapshots.final
    );
  }

  actualServerCoverage(inputIntegrity = this.scriptedMessageInputIntegrity()) {
    if (!(this.scenario && this.scenario.strategyAudit && this.scenario.strategyAudit.enabled)) {
      return null;
    }
    return buildActualServerCoverage(
      this.scenario,
      this.strategyAuditSnapshots.baseline,
      this.strategyAuditSnapshots.final,
      this.events,
      { inputIntegrity }
    );
  }

  p0Batch6Acceptance(summary = null) {
    if (!this.scenario.p0Batch6Acceptance) return null;
    return buildP0Batch6Acceptance({
      scenario: this.scenario,
      summary: summary || this.summary(),
      baseline: this.strategyAuditSnapshots.baseline,
      finalSnapshot: this.strategyAuditSnapshots.final,
      events: this.events,
      errors: this.errors
    });
  }

  recordMessageAttempt(studentId, seq, marker, kind, details = {}) {
    const state = this.ensureStudent(studentId);
    state.messagesAttempted += 1;
    this.counters.messageAttempted += 1;
    this.event('message_attempt', {
      studentId,
      seq,
      messageIndex: details.messageIndex === undefined ? seq : details.messageIndex,
      message_index: details.messageIndex === undefined ? seq : details.messageIndex,
      marker,
      kind,
      clientMessageId: details.clientMessageId || details.client_message_id || null,
      client_message_id: details.clientMessageId || details.client_message_id || null,
      ...details
    });
  }

  recordMessageSuccess({
    studentId,
    seq,
    marker,
    latencyMs,
    messageId,
    kind,
    messageIndex,
    clientMessageId,
    finalStatus,
    deliveryAttemptCount,
    ...details
  }) {
    const state = this.ensureStudent(studentId);
    state.messagesSuccess += 1;
    this.counters.messageSuccess += 1;
    this.counters.messageVisibleToSender += 1;
    this.latencies.messageMs.push(latencyMs);
    this.event('message_success', {
      studentId,
      seq,
      messageIndex: messageIndex === undefined ? seq : messageIndex,
      message_index: messageIndex === undefined ? seq : messageIndex,
      marker,
      latencyMs,
      messageId,
      kind,
      clientMessageId: clientMessageId || null,
      client_message_id: clientMessageId || null,
      finalStatus: finalStatus || 'SUCCESS',
      final_status: finalStatus || 'SUCCESS',
      deliveryAttemptCount: deliveryAttemptCount || 1,
      delivery_attempt_count: deliveryAttemptCount || 1,
      ...details
    });
  }

  recordMessageFailure(studentId, seq, marker, error, kind, details = {}) {
    const state = this.ensureStudent(studentId);
    state.messagesFailed += 1;
    this.counters.messageFailed += 1;
    this.error('message_failure', studentId, error, {
      seq,
      messageIndex: details.messageIndex === undefined ? seq : details.messageIndex,
      message_index: details.messageIndex === undefined ? seq : details.messageIndex,
      marker,
      kind,
      clientMessageId: details.clientMessageId || details.client_message_id || null,
      client_message_id: details.clientMessageId || details.client_message_id || null,
      finalStatus: details.finalStatus || (error && error.deliveryStatus) || 'HTTP_OTHER_ERROR',
      final_status: details.finalStatus || (error && error.deliveryStatus) || 'HTTP_OTHER_ERROR',
      ...details
    });
  }

  recordMessageDeliveryAttempt(data = {}) {
    this.counters.messageDeliveryAttempted += 1;
    if (Number(data.attempt_count || 0) > 1) this.counters.messageDeliveryRetried += 1;
    this.event('message_delivery_attempt', {
      ...data,
      status: data.status || null,
      message_index: data.message_index === undefined ? data.messageIndex : data.message_index,
      client_message_id: data.client_message_id || data.clientMessageId || null
    });
  }

  recordMessageDeliveryFinal(data = {}) {
    this.counters.messageDeliveryFinalized += 1;
    this.event('message_delivery_final', {
      ...data,
      final_status: data.final_status || data.finalStatus || null,
      message_index: data.message_index === undefined ? data.messageIndex : data.message_index,
      client_message_id: data.client_message_id || data.clientMessageId || null
    });
  }

  scriptedMessageInputIntegrity() {
    const messages = this.scenario && this.scenario.scriptedDiscussion &&
      Array.isArray(this.scenario.scriptedDiscussion.messages)
      ? this.scenario.scriptedDiscussion.messages
      : [];
    const keyFor = (studentId, messageIndex, fallback) => (
      `${studentId || ''}:${messageIndex === undefined || messageIndex === null ? fallback : messageIndex}`
    );
    const expectedKeys = new Set(messages.map((message, index) => keyFor(
      message.studentId || message.student_id,
      message.messageIndex === undefined ? (message.seq === undefined ? index + 1 : message.seq) : message.messageIndex,
      index + 1
    )));
    const scriptedEvent = (event) => String(event.kind || '').startsWith('script:');
    const successEvents = this.events.filter((event) => event.type === 'message_success' && scriptedEvent(event));
    const successKeys = successEvents.map((event, index) => keyFor(
      event.studentId,
      event.messageIndex === undefined ? event.message_index === undefined ? event.seq : event.message_index : event.messageIndex,
      index + 1
    ));
    const successfulKeys = new Set(successKeys.filter((key) => expectedKeys.has(key)));
    const failedEvents = this.events.filter((event) => (
      event.type === 'message_failure' ||
      (event.type === 'message_delivery_final' &&
        (event.finalStatus || event.final_status || event.status) !== 'SUCCESS')
    ) && scriptedEvent(event));
    const failedKeys = new Set(failedEvents.map((event, index) => keyFor(
      event.studentId,
      event.messageIndex === undefined
        ? event.message_index === undefined ? event.seq : event.message_index
        : event.messageIndex,
      index + 1
    )));
    const missingKeys = [...expectedKeys].filter((key) => !successfulKeys.has(key));
    const duplicateSuccessKeys = [...new Set(successKeys.filter((key, index) => successKeys.indexOf(key) !== index))];
    const complete = expectedKeys.size === successfulKeys.size &&
      missingKeys.length === 0 && duplicateSuccessKeys.length === 0;
    const result = {
      status: complete ? 'COMPLETE' : 'INPUT_INCOMPLETE',
      coverageAllowed: complete,
      expected_script_messages: expectedKeys.size,
      successful_script_messages: successfulKeys.size,
      attempted_script_messages: new Set(this.events
        .filter((event) => event.type === 'message_attempt' && scriptedEvent(event))
        .map((event, index) => keyFor(
          event.studentId,
          event.messageIndex === undefined
            ? event.message_index === undefined ? event.seq : event.message_index
            : event.messageIndex,
          index + 1
        ))).size,
      failed_script_messages: failedKeys.size,
      missing_message_keys: missingKeys,
      duplicate_success_keys: duplicateSuccessKeys,
      failure_statuses: [...new Set(failedEvents
        .map((event) => event.finalStatus || event.final_status || event.status)
        .filter(Boolean))],
      complete
    };
    return result;
  }

  finalizeScriptedMessageInput() {
    const integrity = this.scriptedMessageInputIntegrity();
    if (!integrity.complete && this.counters.inputIncomplete === 0) {
      this.counters.inputIncomplete = 1;
      this.event('input_incomplete', integrity);
    }
    return integrity;
  }

  recordScroll(studentId) {
    this.counters.scrollActions += 1;
    this.event('scroll_action', { studentId });
  }

  recordPageError(studentId, error) {
    const state = this.ensureStudent(studentId);
    state.pageErrors += 1;
    this.counters.pageErrors += 1;
    this.error('page_error', studentId, error);
  }

  recordConsoleError(studentId, text) {
    const state = this.ensureStudent(studentId);
    state.consoleErrors += 1;
    this.counters.consoleErrors += 1;
    this.error('console_error', studentId, text);
  }

  recordRequestFailure(studentId, url, failure) {
    const state = this.ensureStudent(studentId);
    state.requestFailures += 1;
    this.counters.requestFailures += 1;
    this.error('request_failure', studentId, failure && failure.errorText ? failure.errorText : 'request failed', { url });
  }

  recordHttpError(studentId, url, status) {
    this.counters.httpErrors += 1;
    this.event('http_error', { studentId, url, status });
  }

  recordWebsocketClosed(studentId, url) {
    this.counters.websocketClosed += 1;
    this.event('websocket_closed', { studentId, url });
  }

  recordWebsocketError(studentId, url, error) {
    this.counters.websocketErrors += 1;
    this.error('websocket_error', studentId, error, { url });
  }

  recordStudentFatalError(studentId, error) {
    const state = this.ensureStudent(studentId);
    state.fatalError = normalizeError(error);
    this.counters.studentFatalErrors += 1;
    this.error('student_fatal_error', studentId, error);
  }

  getStopReason(stopConditions = {}) {
    const attemptedLogins = this.counters.loginAttempted || 1;
    const loginFailureRate = this.counters.loginFailed / attemptedLogins;
    if (
      stopConditions.maxLoginFailureRate !== undefined &&
      attemptedLogins >= Math.max(5, Math.floor(this.scenario.totalStudents * 0.25)) &&
      loginFailureRate > stopConditions.maxLoginFailureRate
    ) {
      return `login failure rate ${loginFailureRate.toFixed(3)} exceeded ${stopConditions.maxLoginFailureRate}`;
    }

    const attemptedMessages = this.counters.messageAttempted;
    if (attemptedMessages >= 20 && stopConditions.maxMessageFailureRate !== undefined) {
      const messageFailureRate = this.counters.messageFailed / attemptedMessages;
      if (messageFailureRate > stopConditions.maxMessageFailureRate) {
        return `message failure rate ${messageFailureRate.toFixed(3)} exceeded ${stopConditions.maxMessageFailureRate}`;
      }
    }

    if (
      stopConditions.maxFatalStudentErrors !== undefined &&
      this.counters.studentFatalErrors > stopConditions.maxFatalStudentErrors
    ) {
      return `fatal student errors ${this.counters.studentFatalErrors} exceeded ${stopConditions.maxFatalStudentErrors}`;
    }

    return null;
  }

  summary() {
    const messageAttempted = this.counters.messageAttempted;
    const loginAttempted = this.counters.loginAttempted;
    const plannedScriptedMessages = this.scenario.scriptedDiscussion &&
      Array.isArray(this.scenario.scriptedDiscussion.messages)
      ? this.scenario.scriptedDiscussion.messages.length
      : null;
    const inputIntegrity = this.scriptedMessageInputIntegrity();
    const actualServerCoverage = this.actualServerCoverage(inputIntegrity);
    const coverageStatistics = actualServerCoverage && actualServerCoverage.coverageStatistics;
    return {
      runId: this.runId,
      scenario: this.scenario.name,
      baseUrl: this.scenario.baseUrl,
      resourceMode: this.scenario.resourceMode,
      validationMode: actualServerCoverage
        ? actualServerCoverage.validationMode
        : (this.scenario.strategyAudit && this.scenario.strategyAudit.requireActualCoverage
          ? 'real_coverage_unavailable'
          : 'student_discussion_only'),
      auditAvailable: actualServerCoverage ? actualServerCoverage.auditAvailable : false,
      totalStudents: this.scenario.totalStudents,
      durationMs: this.scenario.discussionDurationMs,
      startedAt: this.startedAt.toISOString(),
      endedAt: this.endedAt ? this.endedAt.toISOString() : null,
      counters: this.counters,
      rates: {
        loginSuccessRate: loginAttempted ? roundRate(this.counters.loginSuccess / loginAttempted) : null,
        messageSuccessRate: messageAttempted ? roundRate(this.counters.messageSuccess / messageAttempted) : null
      },
      input_integrity: inputIntegrity,
      scriptedMessageProgress: plannedScriptedMessages === null ? null : {
        planned: plannedScriptedMessages,
        attempted: this.counters.messageAttempted,
        successful: this.counters.messageSuccess,
        failed: this.counters.messageFailed,
        unsent: Math.max(0, inputIntegrity.expected_script_messages - inputIntegrity.attempted_script_messages),
        incomplete: Math.max(0, inputIntegrity.expected_script_messages - inputIntegrity.successful_script_messages),
        complete: inputIntegrity.complete
      },
      transcripts: this.transcripts.map((transcript) => ({
        groupCode: transcript.groupCode,
        groupId: transcript.groupId,
        capturedBy: transcript.capturedBy,
        capturedAt: transcript.capturedAt,
        messageCount: transcript.messageCount,
        agentMessageCount: transcript.agentMessageCount,
        scriptedStates: transcript.scriptedStates
      })),
      scriptedStateCoverage: buildScriptedStateCoverage(this.scenario, this.events),
      scriptedStrategyCoverage: buildScriptedStrategyCoverage(this.scenario, this.events),
      actualStateCoverage: actualServerCoverage && actualServerCoverage.actualStateCoverage,
      actualStrategyCoverage: actualServerCoverage && actualServerCoverage.actualStrategyCoverage,
      modelFailureCoverage: actualServerCoverage && actualServerCoverage.modelFailureCoverage,
      agentLockCoverage: actualServerCoverage && actualServerCoverage.agentLockCoverage,
      planned_script_coverage: actualServerCoverage && actualServerCoverage.plannedScriptCoverage,
      message_send_coverage: actualServerCoverage && actualServerCoverage.messageSendCoverage,
      canonical_db_coverage: actualServerCoverage && actualServerCoverage.canonicalDbCoverage,
      teacher_api_coverage: actualServerCoverage && actualServerCoverage.teacherApiCoverage,
      export_coverage: actualServerCoverage && actualServerCoverage.exportCoverage,
      intervention_coverage: actualServerCoverage && actualServerCoverage.interventionCoverage,
      optional_support_coverage: actualServerCoverage && actualServerCoverage.optionalSupportCoverage,
      inhibition_coverage: actualServerCoverage && actualServerCoverage.inhibitionCoverage,
      coverage_statistics: coverageStatistics,
      // Keep the camelCase alias for callers that consume the in-process summary.
      coverageStatistics,
      coverage_statistics_report: coverageStatistics
        ? formatCoverageStatisticsMarkdown(coverageStatistics)
        : null,
      actualServerCoveragePassed: actualServerCoverage ? actualServerCoverage.passed : null,
      latencies: {
        loginMs: latencySummary(this.latencies.loginMs),
        messageMs: latencySummary(this.latencies.messageMs),
        aiInputLockWaitMs: latencySummary(this.latencies.aiInputLockWaitMs),
        pipelineWaitMs: latencySummary(this.latencies.pipelineWaitMs),
        leaseDurationMs: latencySummary(this.latencies.leaseDurationMs),
        continuousClientBlockMs: latencySummary(this.latencies.continuousClientBlockMs)
      },
      lock_observation: {
        client_input_lock_restored: this.counters.clientInputLockRestored,
        lock_observer_races: this.counters.lockObserverRaces,
        active_continuous_blocks: [...this.clientLockTrackers.entries()].map(([key, tracker]) => ({
          group_code: key,
          client_block_started_at: tracker.startedAt,
          sequential_lease_count: Math.max(1, tracker.ownerIds.size)
        }))
      },
      students: [...this.students.values()]
    };
  }

  writeReports(reportDir) {
    this.endedAt = new Date();
    ensureDir(reportDir);

    const prefix = path.join(reportDir, this.runId);
    const summary = this.summary();
    let p0Batch6Acceptance = null;
    if (this.scenario.p0Batch6Acceptance) {
      p0Batch6Acceptance = this.p0Batch6Acceptance(summary);
      summary.p0Batch6Acceptance = p0Batch6Acceptance;
    }

    writeJson(`${prefix}-summary.json`, summary);
    fs.writeFileSync(`${prefix}-events.csv`, this.eventsToCsv(), 'utf8');
    fs.writeFileSync(`${prefix}-errors.log`, this.errorsToLog(), 'utf8');
    const transcriptJsonPath = `${prefix}-transcript.json`;
    const transcriptMdPath = `${prefix}-transcript.md`;
    const strategyAuditPath = `${prefix}-strategy-audit.json`;
    writeJson(transcriptJsonPath, {
      runId: this.runId,
      scenario: this.scenario.name,
      baseUrl: this.scenario.baseUrl,
      generatedAt: this.endedAt.toISOString(),
      transcripts: this.transcripts
    });
    fs.writeFileSync(transcriptMdPath, formatTranscriptMarkdown(summary, this.transcripts), 'utf8');
    writeJson(strategyAuditPath, {
      runId: this.runId,
      scenario: this.scenario.name,
      baseUrl: this.scenario.baseUrl,
      generatedAt: this.endedAt.toISOString(),
      coverage: {
        validationMode: summary.validationMode,
        auditAvailable: summary.auditAvailable,
        planned_script_coverage: summary.planned_script_coverage,
        message_send_coverage: summary.message_send_coverage,
        canonical_db_coverage: summary.canonical_db_coverage,
        teacher_api_coverage: summary.teacher_api_coverage,
        export_coverage: summary.export_coverage,
        intervention_coverage: summary.intervention_coverage,
        inhibition_coverage: summary.inhibition_coverage,
        coverage_statistics: summary.coverage_statistics,
        coverage_statistics_report: summary.coverage_statistics_report,
        actualStateCoverage: summary.actualStateCoverage,
        actualStrategyCoverage: summary.actualStrategyCoverage,
        modelFailureCoverage: summary.modelFailureCoverage,
        agentLockCoverage: summary.agentLockCoverage,
        passed: summary.actualServerCoveragePassed
      },
      snapshots: this.strategyAuditSnapshots
    });

    const p0Batch6Bundle = p0Batch6Acceptance
      ? writeP0Batch6Bundle({
          reportDir,
          runId: this.runId,
          scenario: this.scenario,
          summary,
          events: this.events,
          errors: this.errors,
          transcripts: this.transcripts,
          strategyAuditSnapshots: this.strategyAuditSnapshots,
          acceptance: p0Batch6Acceptance
        })
      : null;

    return {
      summaryPath: `${prefix}-summary.json`,
      eventsPath: `${prefix}-events.csv`,
      errorsPath: `${prefix}-errors.log`,
      transcriptJsonPath,
      transcriptMdPath,
      strategyAuditPath,
      p0Batch6Bundle,
      summary
    };
  }

  eventsToCsv() {
    const columns = [
      'ts',
      'type',
      'studentId',
      'profile',
      'scenarioId',
      'phase',
      'plannedState',
      'seq',
      'kind',
      'latencyMs',
      'status',
      'messageId',
      'agentMessageId',
      'marker',
      'groupCode',
      'messageCount',
      'agentMessageCount',
      'url',
      'source',
      'locked',
      'reason',
      'activeInterventionRunId',
      'trigger_message_id',
      'pipeline_run_id',
      'terminal_reason',
      'terminal_status',
      'trigger_message_at',
      'pipeline_terminal_at',
      'pipeline_wait_duration',
      'lease_acquired_at',
      'lease_released_at',
      'lease_duration',
      'client_block_started_at',
      'client_input_restored_at',
      'continuous_block_duration',
      'sequential_lease_count',
      'waitMs',
      'details',
      'error'
    ];
    const rows = [columns.join(',')];
    for (const event of this.events) {
      rows.push(columns.map((col) => csvEscape(event[col])).join(','));
    }
    return `${rows.join('\n')}\n`;
  }

  errorsToLog() {
    if (!this.errors.length) return '';
    return `${this.errors.map((item) => JSON.stringify(item)).join('\n')}\n`;
  }
}

function lockTrackerKey(data = {}) {
  return String(
    data.groupCode ||
    data.group_code ||
    data.groupId ||
    data.group_id ||
    data.studentId ||
    'global'
  );
}

function normalizeMetricTimestamp(value) {
  const parsed = value ? Date.parse(String(value)) : Date.now();
  return Number.isFinite(parsed) ? new Date(parsed).toISOString() : new Date().toISOString();
}

function finiteNonNegative(value) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

function durationBetween(start, end) {
  const started = start ? Date.parse(String(start)) : NaN;
  const finished = end ? Date.parse(String(end)) : NaN;
  if (!Number.isFinite(started) || !Number.isFinite(finished)) return null;
  return Math.max(0, finished - started);
}

function normalizeError(error) {
  if (!error) return 'unknown error';
  if (typeof error === 'string') return error;
  if (error.message) return error.message;
  return JSON.stringify(error);
}

function isRoomAiInterveningError(error, result = {}) {
  const status = Number(result.status || result.statusCode || result.httpStatus || 0);
  const message = normalizeError(error);
  return (
    status === 423 ||
    /ROOM_AI_INTERVENING/i.test(message) ||
    /HTTP\s*423/i.test(message) ||
    /status\s*=\s*423/i.test(message)
  );
}

function roundRate(value) {
  return Math.round(value * 10000) / 10000;
}

function latencySummary(values) {
  return {
    count: values.length,
    avg: avg(values),
    p50: percentile(values, 50),
    p95: percentile(values, 95),
    p99: percentile(values, 99),
    max: values.length ? Math.max(...values) : null
  };
}

function normalizeTranscript(transcript, runId, scenario) {
  const scriptedLookup = buildScriptedMessageLookup(scenario);
  const messages = (transcript.messages || []).map((message) => (
    normalizeTranscriptMessage(message, runId, scriptedLookup)
  ));
  const scriptedStates = [...new Set(messages.map((message) => message.scriptedState).filter(Boolean))];
  return {
    groupId: transcript.groupId,
    groupCode: transcript.groupCode,
    capturedBy: transcript.capturedBy,
    capturedAt: transcript.capturedAt,
    latestId: transcript.latestId,
    messageCount: messages.length,
    agentMessageCount: messages.filter((message) => message.role === 'agent').length,
    scriptedStates,
    messages
  };
}

function normalizeTranscriptMessage(message, runId, scriptedLookup) {
  const role = message.resolved_role || message.role || '';
  const content = String(message.content || '');
  const marker = parseLoadScriptMarker(content, runId);
  const cleanContent = marker ? content.replace(marker.raw, '').trim() : stripLoadMarkers(content).trim();
  const scriptedMatch = marker || findScriptedMessage(message, cleanContent, scriptedLookup);
  const syntheticMarker = !marker && scriptedMatch && runId
    ? `[LOAD_SCRIPT:${runId}:${scriptedMatch.studentId}:${scriptedMatch.seq}:${scriptedMatch.state}]`
    : null;
  return {
    id: message.id,
    sequence: message.sequence,
    createdAt: message.created_at || message.createdAt || null,
    role,
    userId: message.user_id,
    participantCode: message.participant_code || '',
    displayName: message.display_name || message.real_name || message.username || '',
    marker: marker ? marker.raw : syntheticMarker,
    scriptedState: scriptedMatch ? scriptedMatch.state : null,
    scriptedSeq: scriptedMatch ? scriptedMatch.seq : null,
    content: cleanContent || content
  };
}

function parseLoadScriptMarker(content, runId) {
  const match = String(content || '').match(/\[LOAD_SCRIPT:([^:\]]+):([^:\]]+):(\d+):([^\]]+)\]/);
  if (!match) return null;
  if (runId && match[1] !== runId) return null;
  return {
    raw: match[0],
    runId: match[1],
    studentId: match[2],
    seq: Number(match[3]),
    state: match[4]
  };
}

function buildScriptedMessageLookup(scenario) {
  const messages = scenario && scenario.scriptedDiscussion && Array.isArray(scenario.scriptedDiscussion.messages)
    ? scenario.scriptedDiscussion.messages
    : [];
  const byStudentContent = new Map();
  const byContent = new Map();

  for (const message of messages) {
    const content = normalizeContentForMatch(message.text);
    if (!content) continue;
    const entry = {
      studentId: message.studentId || message.student || message.participantCode || '',
      seq: message.seq,
      state: message.state || 'script'
    };
    byStudentContent.set(`${entry.studentId}\n${content}`, entry);
    const current = byContent.get(content);
    if (!current) {
      byContent.set(content, entry);
    } else if (Array.isArray(current)) {
      current.push(entry);
    } else {
      byContent.set(content, [current, entry]);
    }
  }

  return { byStudentContent, byContent };
}

function findScriptedMessage(message, content, scriptedLookup) {
  if (!scriptedLookup) return null;
  const role = String(message.resolved_role || message.role || '').toLowerCase();
  if (role === 'agent' || role === 'teacher' || role === 'system') return null;

  const normalized = normalizeContentForMatch(content);
  if (!normalized) return null;

  const ids = [
    message.participant_code,
    message.display_name,
    message.real_name,
    message.username
  ].filter(Boolean);
  for (const id of ids) {
    const match = scriptedLookup.byStudentContent.get(`${id}\n${normalized}`);
    if (match) return match;
  }

  const contentMatch = scriptedLookup.byContent.get(normalized);
  return contentMatch && !Array.isArray(contentMatch) ? contentMatch : null;
}

function normalizeContentForMatch(value) {
  return stripLoadMarkers(value).replace(/\s+/g, ' ').trim();
}

function stripLoadMarkers(value) {
  return String(value || '').replace(/\[(?:LOAD_TEST|LOAD_SCRIPT):[^\]]+\]\s*/g, '');
}

function formatTranscriptMarkdown(summary, transcripts) {
  const lines = [
    `# Conversation Transcript: ${summary.runId}`,
    '',
    `Scenario: ${summary.scenario}`,
    `Base URL: ${summary.baseUrl}`,
    `Generated at: ${summary.endedAt}`,
    ''
  ];

  if (!transcripts.length) {
    lines.push('No conversation transcripts were captured.');
    lines.push('');
    return lines.join('\n');
  }

  for (const transcript of transcripts) {
    lines.push(`## Group ${transcript.groupCode}`);
    lines.push('');
    lines.push(`Captured by: ${transcript.capturedBy}`);
    lines.push(`Messages: ${transcript.messageCount}`);
    lines.push(`Agent messages: ${transcript.agentMessageCount}`);
    lines.push(`Scripted states: ${transcript.scriptedStates.join(', ') || 'none'}`);
    lines.push('');
    lines.push('| Time | Seq | Role | Speaker | Script State | Content |');
    lines.push('| --- | ---: | --- | --- | --- | --- |');
    for (const message of transcript.messages) {
      lines.push([
        markdownCell(message.createdAt || ''),
        markdownCell(message.sequence || ''),
        markdownCell(message.role || ''),
        markdownCell(message.displayName || message.participantCode || message.userId || ''),
        markdownCell(message.scriptedState || ''),
        markdownCell(message.content || '')
      ].join(' | ').replace(/^/, '| ').replace(/$/, ' |'));
    }
    lines.push('');
  }

  return lines.join('\n');
}

function markdownCell(value) {
  return String(value === undefined || value === null ? '' : value)
    .replace(/\r?\n/g, '<br>')
    .replace(/\|/g, '\\|');
}

module.exports = {
  Metrics,
  normalizeError,
  isRoomAiInterveningError
};
