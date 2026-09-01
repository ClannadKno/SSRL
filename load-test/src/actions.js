const { absoluteUrl, chance, pickRandom, randomBetween, randomInt, sleep } = require('./utils');
const {
  MESSAGE_SEND_STATUS,
  beginMessageDeliveryAttempt,
  classifyMessageSendFailure,
  completeMessageDeliveryAttempt,
  createMessageDeliveryRecord,
  finalizeMessageDelivery,
  isComposerLocked,
  isRetryableMessageSendStatus,
  messageDeliveryError,
  messageRetryBackoffMs,
  serializeMessageDelivery
} = require('./messageDelivery');
const {
  pipelineCandidates,
  pipelineRunId,
  pipelineTerminal,
  pipelineTiming,
  selectPipeline
} = require('./expectedGate');

async function login(page, student, scenario) {
  const startedAt = Date.now();
  const loginKey = student.loginKey || student.login_key || student.key;
  if (!loginKey) {
    throw new Error(`student ${student.id} is missing loginKey`);
  }

  const loginUrl = absoluteUrl(scenario.baseUrl, scenario.loginPath);
  const totalTimeoutMs = Math.max(1000, Number(scenario.timeouts.loginMs || 45_000));
  const maxAttempts = Math.max(
    1,
    Number((scenario.flow && scenario.flow.loginAttempts) || 3)
  );
  const retryDelayMs = Math.max(
    0,
    Number((scenario.flow && scenario.flow.loginRetryDelayMs) || 500)
  );
  let lastError = null;

  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    const elapsedMs = Date.now() - startedAt;
    const remainingMs = Math.max(0, totalTimeoutMs - elapsedMs);
    if (!remainingMs) break;
    const attemptTimeoutMs = Math.max(
      1000,
      Math.min(remainingMs, Math.ceil(totalTimeoutMs / maxAttempts))
    );
    try {
      await loginAttempt(page, loginKey, scenario, loginUrl, attemptTimeoutMs);
      const currentUrl = page.url();
      return {
        latencyMs: Date.now() - startedAt,
        url: currentUrl,
        attemptCount: attempt
      };
    } catch (error) {
      lastError = error;
      if (attempt >= maxAttempts || Date.now() - startedAt >= totalTimeoutMs) break;
      await page.waitForTimeout(retryDelayMs).catch(() => sleep(retryDelayMs));
    }
  }

  const detail = lastError && lastError.message ? lastError.message : 'login timeout exhausted';
  throw new Error(`login failed after ${maxAttempts} bounded attempts: ${detail}`);
}

async function loginAttempt(page, loginKey, scenario, loginUrl, timeoutMs) {
  await page.goto(loginUrl, {
    waitUntil: 'domcontentloaded',
    timeout: Math.min(timeoutMs, Number(scenario.timeouts.navigationMs || timeoutMs))
  });

  if (new URL(page.url()).pathname !== '/login') return;

  await page.locator(scenario.selectors.loginKeyInput).first().fill(loginKey, {
    timeout: timeoutMs
  });

  const responsePromise = typeof page.waitForResponse === 'function'
    ? page.waitForResponse((response) => {
      let pathname = '';
      try {
        pathname = new URL(response.url()).pathname;
      } catch (_error) {
        return false;
      }
      return response.request().method() === 'POST' && pathname === new URL(loginUrl).pathname;
    }, { timeout: timeoutMs })
    : null;

  await page.locator(scenario.selectors.loginSubmit).first().click({
    timeout: timeoutMs,
    noWaitAfter: true
  });

  const response = responsePromise ? await responsePromise : null;
  const status = response ? Number(response.status()) : null;
  if (status !== null && status >= 400) {
    const diagnostic = await pageDiagnostic(page);
    throw new Error(`login POST returned HTTP ${status}: ${diagnostic}`);
  }

  try {
    await page.waitForFunction(() => window.location.pathname !== '/login', null, {
      timeout: timeoutMs
    });
  } catch (error) {
    const headers = response && typeof response.headers === 'function' ? response.headers() : {};
    const location = headers && (headers.location || headers.Location);
    if (!location || (status !== null && status >= 400)) throw error;
    await page.goto(absoluteUrl(scenario.baseUrl, location), {
      waitUntil: 'domcontentloaded',
      timeout: timeoutMs
    });
  }
  await page.waitForLoadState('domcontentloaded', { timeout: timeoutMs }).catch(() => null);

  if (new URL(page.url()).pathname === '/login') {
    const diagnostic = await pageDiagnostic(page);
    throw new Error(`login stayed on /login: ${diagnostic}`);
  }
}

async function enterDiscussion(page, scenario) {
  const target = absoluteUrl(scenario.baseUrl, scenario.discussionPath);
  const targetPath = new URL(target).pathname;
  const selectors = [
    scenario.selectors.discussionInput,
    scenario.selectors.chatBox,
    scenario.selectors.collaborativeEditorArea,
    scenario.selectors.loginKeyInput
  ].join(', ');
  const totalTimeoutMs = Math.max(1000, Number(scenario.timeouts.discussionEnterMs || 60_000));
  const maxAttempts = Math.max(
    3,
    Number((scenario.flow && scenario.flow.discussionEnterAttempts) || 3)
  );
  const retryDelayMs = Math.max(
    250,
    Number((scenario.flow && scenario.flow.discussionEnterRetryDelayMs) || 500)
  );
  const startedAt = Date.now();
  let lastError = null;

  // The waiting-room page redirects itself when the fourth member makes the
  // group runnable. That redirect can cancel an in-flight Playwright goto even
  // though the student was successfully recorded as ready. Some browsers also
  // stay on a stale student phase until a manual refresh; keep re-entering the
  // discussion route while the login session is still valid.
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    const remainingMs = Math.max(1000, totalTimeoutMs - (Date.now() - startedAt));
    const attemptTimeoutMs = Math.max(
      1000,
      Math.min(remainingMs, Math.ceil(totalTimeoutMs / maxAttempts))
    );
    try {
      await page.goto(target, {
        waitUntil: 'domcontentloaded',
        timeout: attemptTimeoutMs
      });
      await page.waitForSelector(selectors, { timeout: attemptTimeoutMs });
      lastError = null;
      break;
    } catch (error) {
      lastError = error;
      let currentPath = '';
      try {
        currentPath = new URL(page.url()).pathname;
      } catch (_urlError) {
        currentPath = '';
      }
      const canRetryStudentPage = (
        currentPath === targetPath ||
        currentPath.startsWith('/student/')
      );
      if (
        currentPath === '/login' ||
        !canRetryStudentPage ||
        attempt === maxAttempts ||
        Date.now() - startedAt >= totalTimeoutMs
      ) {
        throw error;
      }
      await page.waitForTimeout(retryDelayMs);
    }
  }

  if (lastError) throw lastError;

  if (new URL(page.url()).pathname === '/login') {
    throw new Error('redirected to login when entering discussion');
  }

  const details = await page.evaluate(() => ({
    groupId: typeof window.GROUP_ID !== 'undefined' ? window.GROUP_ID : null,
    participantCode: typeof window.PARTICIPANT_CODE !== 'undefined' ? window.PARTICIPANT_CODE : null,
    mode: typeof window.MODE !== 'undefined' ? window.MODE : null
  })).catch(() => ({}));

  return {
    url: page.url(),
    ...details
  };
}

async function submitQuestionnaires(page, scenario, student, stage, metrics) {
  const payload = await browserFetchJson(page, `/api/student/questionnaires?stage=${encodeURIComponent(stage)}`, {
    timeoutMs: scenario.timeouts.questionnaireMs
  });
  const questionnaires = payload.data.questionnaires || [];
  const submitted = [];

  for (const questionnaire of questionnaires) {
    const completed = questionnaire.completed_stages || [];
    if (completed.includes(stage)) continue;

    const questionnaireId = questionnaire.id;
    metrics.recordQuestionnaireAttempt(student.id, stage, questionnaireId);
    try {
      const responses = buildQuestionnaireResponses(questionnaire, student, stage);
      await browserFetchJson(page, `/api/student/questionnaires/${questionnaireId}/responses`, {
        method: 'POST',
        body: {
          response_stage: stage,
          responses
        },
        timeoutMs: scenario.timeouts.questionnaireMs,
        okStatuses: [200, 409]
      });
      metrics.recordQuestionnaireSuccess(student.id, stage, questionnaireId);
      submitted.push(questionnaireId);
    } catch (error) {
      metrics.recordQuestionnaireFailure(student.id, stage, questionnaireId, error);
      throw error;
    }
  }

  return submitted;
}

async function submitCheckin(page, scenario, student, checkinType, note, overrides = {}) {
  const groupId = await resolveGroupId(page, student);
  return browserFetchJson(page, '/api/checkin', {
    method: 'POST',
    body: {
      group_id: groupId,
      emotion_option: overrides.emotion_option || overrides.emotionOption || pickRandom(['smooth', 'stuck', 'conflict', 'silent', 'frustrated']),
      positivity: overrides.positivity === undefined ? randomInt(2, 5) : Number(overrides.positivity),
      engagement: overrides.engagement === undefined ? randomInt(2, 5) : Number(overrides.engagement),
      atmosphere: overrides.atmosphere === undefined ? randomInt(2, 5) : Number(overrides.atmosphere),
      expression_willingness: overrides.expression_willingness === undefined
        ? (overrides.expressionWillingness === undefined ? randomInt(2, 5) : Number(overrides.expressionWillingness))
        : Number(overrides.expression_willingness),
      note,
      checkin_type: checkinType
    },
    timeoutMs: scenario.timeouts.apiResponseMs
  });
}

async function requestStudentHelp(page, scenario, student, text) {
  const groupId = await resolveGroupId(page, student);
  return browserFetchJson(page, '/api/student/help', {
    method: 'POST',
    body: {
      group_id: groupId,
      request_text: text,
      client_message_id: `help-${student.id}-${Date.now()}-${Math.random().toString(16).slice(2)}`
    },
    timeoutMs: scenario.timeouts.apiResponseMs,
    okStatuses: [200, 202]
  });
}

async function submitPostEmotionCheckin(page, scenario, student) {
  const groupId = await resolveGroupId(page, student);
  return browserFetchJson(page, '/api/checkin', {
    method: 'POST',
    body: {
      group_id: groupId,
      emotion_option: pickRandom(['smooth', 'stuck', 'conflict']),
      positivity: randomInt(3, 5),
      engagement: randomInt(3, 5),
      atmosphere: randomInt(3, 5),
      expression_willingness: randomInt(3, 5),
      note: `Post discussion check-in from ${student.id}`,
      checkin_type: 'post'
    },
    timeoutMs: scenario.timeouts.questionnaireMs,
    okStatuses: [200, 409]
  });
}

async function fetchGroupTranscript(page, scenario, student) {
  const groupId = await resolveGroupId(page, student);
  const payload = await browserFetchJson(page, `/api/group/${groupId}/messages?limit=120`, {
    timeoutMs: scenario.timeouts.apiResponseMs
  });
  const messages = payload.data.messages || [];
  return {
    groupId,
    groupCode: student.group_code || student.groupCode || `G${student.group_no || ''}`,
    capturedBy: student.id,
    capturedAt: new Date().toISOString(),
    latestId: payload.data.latest_id || null,
    messages
  };
}

async function submitGroupDeliverable(page, scenario, student, runId) {
  const current = await browserFetchJson(page, '/api/collaborative-documents/current', {
    timeoutMs: scenario.timeouts.groupSubmitMs
  });
  const doc = current.data.document;
  if (!doc || !doc.id) {
    throw new Error(`current document missing for ${student.id}: ${JSON.stringify(current.data)}`);
  }

  if (doc.status === 'submitted') {
    return { alreadySubmitted: true, documentId: doc.id };
  }

  const contentText = buildDeliverableText(student, runId);
  const contentHtml = contentText
    .split('\n')
    .map((line) => `<p>${escapeHtml(line)}</p>`)
    .join('');
  const contentJson = JSON.stringify({
    type: 'doc',
    content: contentText.split('\n').filter(Boolean).map((line) => ({
      type: 'paragraph',
      content: [{ type: 'text', text: line }]
    }))
  });

  const response = await browserFetchJson(page, `/api/collaborative-documents/${doc.id}/submit`, {
    method: 'POST',
    body: {
      content_text: contentText,
      content_html: contentHtml,
      content_json: contentJson
    },
    timeoutMs: scenario.timeouts.groupSubmitMs,
    okStatuses: [200, 409]
  });

  return {
    documentId: doc.id,
    status: response.status,
    submissionId: response.data.submission_id || null
  };
}

async function waitForDiscussionReady(page, scenario) {
  const totalTimeoutMs = Math.max(1000, Number(scenario.timeouts.discussionReadyMs || 60_000));
  const maxRefreshes = Math.max(
    0,
    Number((scenario.flow && scenario.flow.discussionReadyRefreshAttempts) || 0)
  );
  const refreshAfterMs = Math.max(
    1000,
    Number((scenario.flow && scenario.flow.discussionReadyRefreshAfterMs) || 15_000)
  );
  const startedAt = Date.now();
  let lastError = null;

  for (let attempt = 0; attempt <= maxRefreshes; attempt += 1) {
    const elapsedMs = Date.now() - startedAt;
    const remainingMs = Math.max(1000, totalTimeoutMs - elapsedMs);
    const attemptTimeoutMs = attempt < maxRefreshes
      ? Math.min(refreshAfterMs, remainingMs)
      : remainingMs;
    try {
      const ready = await waitForDiscussionControls(page, scenario, attemptTimeoutMs);
      return {
        ...ready,
        readyRefreshCount: attempt
      };
    } catch (error) {
      lastError = error;
      if (attempt >= maxRefreshes || Date.now() - startedAt >= totalTimeoutMs) break;
      await refreshDiscussionPage(page, scenario);
    }
  }

  throw lastError || new Error('discussion ready timeout');
}

async function waitForDiscussionControls(page, scenario, timeoutMs) {
  await page.waitForFunction((selector) => {
    const el = document.querySelector(selector);
    if (!el) return false;
    const style = window.getComputedStyle(el);
    return style && style.visibility !== 'hidden' && style.display !== 'none' && !el.disabled;
  }, scenario.selectors.discussionInput, {
    timeout: timeoutMs
  });

  await page.waitForSelector(scenario.selectors.chatBox, {
    timeout: timeoutMs
  });

  return page.evaluate(() => ({
    groupId: typeof window.GROUP_ID !== 'undefined' ? window.GROUP_ID : null,
    mode: typeof window.MODE !== 'undefined' ? window.MODE : null
  })).catch(() => ({}));
}

async function refreshDiscussionPage(page, scenario) {
  const target = absoluteUrl(scenario.baseUrl, scenario.discussionPath);
  const targetPath = new URL(target).pathname;
  let currentPath = '';
  try {
    currentPath = new URL(page.url()).pathname;
  } catch (_error) {
    currentPath = '';
  }
  if (currentPath === '/login') {
    throw new Error('redirected to login while waiting for discussion ready');
  }
  if (currentPath === targetPath && page.reload) {
    await page.reload({
      waitUntil: 'domcontentloaded',
      timeout: scenario.timeouts.navigationMs
    });
  } else {
    await page.goto(target, {
      waitUntil: 'domcontentloaded',
      timeout: scenario.timeouts.navigationMs
    });
  }
  await page.waitForLoadState('domcontentloaded', {
    timeout: scenario.timeouts.navigationMs
  }).catch(() => null);
}

async function sendDiscussionMessage(page, scenario, message, observer = {}) {
  const maxRetries = Number(
    (scenario.flow && scenario.flow.aiInputLockSendRetries) === undefined
      ? 2
      : scenario.flow.aiInputLockSendRetries
  );
  const delivery = createMessageDeliveryRecord(message);
  let retries = 0;

  while (true) {
    const attempt = beginMessageDeliveryAttempt(delivery);
    try {
      const composer = await readComposerState(page, scenario);
      if (composer.available && isComposerLocked(composer)) {
        throw messageDeliveryError(
          MESSAGE_SEND_STATUS.UI_INPUT_LOCKED,
          'discussion composer is locked before fill',
          { composer }
        );
      }

      const result = await sendDiscussionMessageOnce(page, scenario, message);
      completeMessageDeliveryAttempt(delivery, attempt, MESSAGE_SEND_STATUS.SUCCESS, {
        message_id: result.messageId,
        http_status: result.status
      });
      const serialized = finalizeMessageDelivery(delivery, MESSAGE_SEND_STATUS.SUCCESS);
      recordMessageDeliveryAttempt(observer, serialized, attempt);
      recordMessageDeliveryFinal(observer, serialized);
      return { ...result, delivery: serialized };
    } catch (error) {
      const status = classifyMessageSendFailure(error);
      completeMessageDeliveryAttempt(delivery, attempt, status, {
        error: error && error.message,
        http_status: error && error.status
      });
      recordMessageDeliveryAttempt(observer, serializeMessageDelivery(delivery), attempt);

      const canRetry = isRetryableMessageSendStatus(status) && retries < maxRetries;
      if (!canRetry) {
        const serialized = finalizeMessageDelivery(delivery, status);
        recordMessageDeliveryFinal(observer, serialized);
        error.deliveryStatus = status;
        error.delivery = serialized;
        throw error;
      }

      retries += 1;
      try {
        await waitForMessageInputReady(
          page,
          scenario,
          message,
          observer,
          status,
          `send_retry_${retries}`
        );
      } catch (waitError) {
        const waitStatus = classifyMessageSendFailure(waitError, {
          stage: 'input_locked'
        });
        const serialized = finalizeMessageDelivery(delivery, waitStatus);
        recordMessageDeliveryFinal(observer, serialized);
        waitError.deliveryStatus = waitStatus;
        waitError.delivery = serialized;
        throw waitError;
      }
    }
  }
}

async function sendDiscussionMessageOnce(page, scenario, message) {
  const visibleText = message.visibleText || message.content || message.marker || '';
  const beforeCount = await page.locator(scenario.selectors.messageText).count().catch(() => null);

  try {
    await page.locator(scenario.selectors.discussionInput).first().fill(message.content, {
      timeout: scenario.timeouts.apiResponseMs
    });
  } catch (error) {
    const composer = await readComposerState(page, scenario);
    if (composer.available && isComposerLocked(composer)) {
      throw messageDeliveryError(
        MESSAGE_SEND_STATUS.UI_INPUT_LOCKED,
        'discussion composer became locked during fill',
        { composer, cause: error }
      );
    }
    throw messageDeliveryError(
      MESSAGE_SEND_STATUS.UI_FILL_FAILED,
      `discussion composer fill failed: ${error.message}`,
      { cause: error }
    );
  }

  let status;
  let body;
  if (message.clientMessageId || message.client_message_id) {
    const result = await postDiscussionMessageWithClientId(page, scenario, message);
    status = result.status;
    body = result.body;
    if (!result.ok || (body && body.ok === false)) {
      throw messageApiError(status, body);
    }
    await clearComposerAndSync(page, scenario);
  } else {
    const responsePromise = page.waitForResponse((response) => {
      const request = response.request();
      return request.method() === 'POST' && response.url().includes('/api/message');
    }, {
      timeout: scenario.timeouts.apiResponseMs
    }).catch(() => null);

    await page.locator(scenario.selectors.sendButton).first().click({
      timeout: scenario.timeouts.apiResponseMs
    });

    const response = await responsePromise;
    if (!response) {
      throw messageDeliveryError(
        MESSAGE_SEND_STATUS.HTTP_OTHER_ERROR,
        'timed out waiting for /api/message response'
      );
    }

    status = response.status();
    try {
      body = await response.json();
    } catch (_err) {
      body = null;
    }

    if (!response.ok() || (body && body.ok === false)) {
      throw messageApiError(status, body);
    }
  }

  try {
    await waitForMessageVisible(page, scenario, visibleText, beforeCount);
  } catch (error) {
    throw messageDeliveryError(
      MESSAGE_SEND_STATUS.MESSAGE_NOT_VISIBLE,
      `message did not become visible: ${error.message}`,
      { cause: error, status }
    );
  }

  return {
    status,
    messageId: body && body.message_id ? body.message_id : null
  };
}

function messageApiError(status, body) {
  const error = new Error(`message API failed: status=${status} body=${JSON.stringify(body)}`);
  error.status = status;
  error.body = body;
  error.code = body && body.code;
  error.deliveryStatus = classifyMessageSendFailure(error, { status, code: error.code });
  return error;
}

async function postDiscussionMessageWithClientId(page, scenario, message) {
  let groupId;
  try {
    groupId = await resolveGroupId(page, {});
  } catch (error) {
    throw messageDeliveryError(
      MESSAGE_SEND_STATUS.DATABASE_NOT_FOUND,
      `group could not be resolved before message send: ${error.message}`,
      { cause: error }
    );
  }

  try {
    return await page.evaluate(async ({ groupId, content, clientMessageId, timeoutMs }) => {
      const token = new URLSearchParams(window.location.search).get('tab_token') || '';
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      const headers = { Accept: 'application/json', 'Content-Type': 'application/json' };
      if (token) headers['X-Tab-Token'] = token;
      try {
        const response = await fetch('/api/message', {
          method: 'POST',
          headers,
          body: JSON.stringify({ group_id: groupId, content, client_message_id: clientMessageId }),
          signal: controller.signal
        });
        const text = await response.text();
        let body = null;
        try {
          body = text ? JSON.parse(text) : null;
        } catch (_error) {
          body = { raw: text.slice(0, 500) };
        }
        return { status: response.status, ok: response.ok, body };
      } finally {
        clearTimeout(timer);
      }
    }, {
      groupId,
      content: message.content,
      clientMessageId: message.clientMessageId || message.client_message_id,
      timeoutMs: scenario.timeouts.apiResponseMs
    });
  } catch (error) {
    throw messageDeliveryError(
      MESSAGE_SEND_STATUS.HTTP_OTHER_ERROR,
      `message API request failed: ${error.message}`,
      { cause: error }
    );
  }
}

async function clearComposerAndSync(page, scenario) {
  await page.evaluate((selector) => {
    const input = document.querySelector(selector);
    if (input) input.value = '';
    if (typeof runStudentSync === 'function') return runStudentSync();
    return null;
  }, scenario.selectors.discussionInput).catch(() => null);
}

function shouldVerifyAiInputLock(scenario) {
  return Boolean(scenario && scenario.flow && scenario.flow.verifyAiInputLock);
}

function isRoomAiInterveningError(error) {
  const status = Number(error && (error.status || error.statusCode || error.httpStatus || 0));
  const message = error && error.message ? error.message : String(error || '');
  const code = error && (error.code || (error.body && error.body.code));
  return (
    status === 423 ||
    code === 'ROOM_AI_INTERVENING' ||
    /ROOM_AI_INTERVENING/i.test(message) ||
    /HTTP\s*423/i.test(message) ||
    /status\s*=\s*423/i.test(message)
  );
}

function recordAiLockMetric(observer, method, payload = {}) {
  if (!observer || !observer.metrics || typeof observer.metrics[method] !== 'function') return;
  observer.metrics[method]({
    studentId: observer.studentId,
    groupCode: observer.groupCode,
    ...payload
  });
}

function recordMessageDeliveryAttempt(observer, delivery, attempt) {
  if (!observer || !observer.metrics || typeof observer.metrics.recordMessageDeliveryAttempt !== 'function') return;
  const currentAttempt = (delivery.attempts || []).find(
    (item) => item.attempt_count === attempt.attempt_count
  ) || attempt;
  observer.metrics.recordMessageDeliveryAttempt({
    studentId: observer.studentId,
    groupCode: observer.groupCode,
    scenarioId: observer.scenarioId || null,
    message_index: delivery.message_index,
    client_message_id: delivery.client_message_id,
    attempt_count: delivery.attempt_count,
    first_attempt_at: delivery.first_attempt_at,
    last_attempt_at: delivery.last_attempt_at,
    status: currentAttempt.status,
    http_status: currentAttempt.http_status,
    error: currentAttempt.error
  });
}

function recordMessageDeliveryFinal(observer, delivery) {
  if (!observer || !observer.metrics || typeof observer.metrics.recordMessageDeliveryFinal !== 'function') return;
  observer.metrics.recordMessageDeliveryFinal({
    studentId: observer.studentId,
    groupCode: observer.groupCode,
    scenarioId: observer.scenarioId || null,
    ...delivery
  });
}

async function readComposerState(page, scenario) {
  const selector = scenario && scenario.selectors && scenario.selectors.discussionInput;
  if (!selector) return { available: false, locked: false };
  return page.evaluate((inputSelector) => {
    const input = document.querySelector(inputSelector);
    if (!input) return { available: false, locked: false };
    return {
      available: true,
      readOnly: Boolean(input.readOnly),
      disabled: Boolean(input.disabled),
      ariaDisabled: input.getAttribute('aria-disabled'),
      className: typeof input.className === 'string'
        ? input.className
        : (input.className && input.className.baseVal) || '',
      locked: Boolean(
        input.readOnly ||
        input.disabled ||
        input.getAttribute('aria-disabled') === 'true' ||
        String(input.className || '').includes('ai-locked')
      )
    };
  }, selector).catch(() => ({ available: false, locked: false }));
}

async function waitForComposerEditable(page, scenario, observer, source, options = {}) {
  const startedAt = Date.now();
  const timeoutMs = Math.max(
    50,
    Number(
      options.timeoutMs ||
      (scenario.timeouts && scenario.timeouts.messageInputMaxWaitMs) ||
      (scenario.timeouts && scenario.timeouts.aiInputLockMaxWaitMs) ||
      90 * 1000
    )
  );
  const initialPollMs = Math.max(
    50,
    Number((scenario.flow && scenario.flow.messageInputPollMs) ||
      (scenario.flow && scenario.flow.aiInputLockPollMs) || 250)
  );
  const maxPollMs = Math.max(
    initialPollMs,
    Number((scenario.flow && scenario.flow.messageInputMaxPollMs) || 3000)
  );
  let pollMs = initialPollMs;
  let lockedObserved = false;

  while (Date.now() - startedAt < timeoutMs) {
    const state = await readComposerState(page, scenario);
    if (!state.available) return 0;
    if (!isComposerLocked(state)) {
      const waitMs = Date.now() - startedAt;
      if (lockedObserved) {
        recordAiLockMetric(observer, 'recordAiInputLockWait', { source, waitMs });
        recordAiLockMetric(observer, 'recordAiInputLockRestored', {
          source,
          client_input_restored_at: new Date().toISOString()
        });
      }
      return waitMs;
    }

    lockedObserved = true;
    await page.evaluate(() => {
      if (typeof runStudentSync === 'function') return runStudentSync();
      return null;
    }).catch(() => null);
    const remainingMs = timeoutMs - (Date.now() - startedAt);
    if (remainingMs <= 0) break;
    await sleep(Math.min(pollMs, remainingMs));
    pollMs = Math.min(maxPollMs, pollMs * 2);
  }

  const waitMs = Date.now() - startedAt;
  if (options.allowObserverRace) {
    recordAiLockMetric(observer, 'recordLockObserverRace', {
      source,
      phase: 'ui_sync_grace_exhausted',
      ui_sync_delay_ms: waitMs
    });
    return waitMs;
  }
  throw messageDeliveryError(
    MESSAGE_SEND_STATUS.UI_INPUT_LOCKED,
    `discussion composer stayed locked for ${waitMs}ms`,
    { waitMs }
  );
}

async function waitForMessageInputReady(page, scenario, message, observer, status, source) {
  const inputLockState = await getAiLockState(page, scenario).catch(() => ({ locked: false }));
  const shouldUseAuthoritativeLockObserver = shouldVerifyAiInputLock(scenario);
  let authoritativeUnlockObserved = false;

  if (inputLockState.locked) {
    if (shouldUseAuthoritativeLockObserver) {
      const result = await handleObservedAiLock(page, scenario, message, observer, source);
      authoritativeUnlockObserved = Boolean(result && result.serverUnlocked);
    } else {
      const result = await waitForAiUnlock(page, scenario, observer, source);
      authoritativeUnlockObserved = Boolean(result && result.serverUnlocked);
    }
  } else if (status === MESSAGE_SEND_STATUS.HTTP_423_LOCKED) {
    await sleep(messageRetryBackoffMs(
      observer && observer.metrics && observer.metrics.counters
        ? observer.metrics.counters.messageDeliveryAttempted + 1
        : 1,
      {
        baseMs: Number((scenario.flow && scenario.flow.messageRetryBaseMs) || 250),
        maxMs: Number((scenario.flow && scenario.flow.messageRetryMaxMs) || 5000)
      }
    ));
  }

  await waitForComposerEditable(page, scenario, observer, source, {
    timeoutMs: authoritativeUnlockObserved
      ? Number((scenario.timeouts && scenario.timeouts.aiInputLockObserverGraceMs) || 5000)
      : undefined,
    allowObserverRace: authoritativeUnlockObserved
  });
}

async function waitIfAiLockedBeforeSend(page, scenario, message, observer) {
  const lock = await getAiLockState(page, scenario).catch(() => ({ locked: false }));
  if (!lock.locked) return;
  await handleObservedAiLock(page, scenario, message, observer, 'pre_send');
}

async function handleObservedAiLock(page, scenario, message, observer, source) {
  const lock = await getAiLockState(page, scenario).catch(() => ({ locked: true, reason: 'ROOM_AI_INTERVENING' }));
  recordAiLockMetric(observer, 'recordAiInputLockObserved', {
    source,
    locked: Boolean(lock.locked),
    reason: lock.reason || null,
    activeInterventionRunId: lock.active_intervention_run_id || null,
    lock_owner_pipeline_run_id: lock.lock_owner_run_id || null
  });

  if (!lock.locked) {
    recordAiLockMetric(observer, 'recordAiInputLockWait', { source, waitMs: 0 });
    return { serverUnlocked: true, waitMs: 0 };
  }

  await assertComposerLocked(page, scenario, observer, source);

  if (scenario.flow && scenario.flow.verifyAiInputLockApiReject !== false) {
    await assertAiLockApiRejected(page, scenario, message, observer, source);
  }

  return waitForAiUnlock(page, scenario, observer, source);
}

async function getAiLockState(page, scenario) {
  const response = await browserFetchJson(page, '/api/student/sync?limit=1', {
    timeoutMs: (scenario.timeouts && scenario.timeouts.apiResponseMs) || 20000
  });
  const body = response.data || {};
  const lock = body.ai_lock || {};
  const room = body.room || {};
  return {
    locked: Boolean(lock.locked || room.state === 'AI_INTERVENING'),
    reason: lock.reason || (room.state === 'AI_INTERVENING' ? 'ROOM_AI_INTERVENING' : null),
    active_intervention_run_id: lock.active_intervention_run_id || room.active_intervention_run_id || null,
    lock_expires_at: lock.lock_expires_at || room.lock_expires_at || null,
    lock_owner_run_id: lock.lock_owner_run_id || null,
    lock_owner_status: lock.lock_owner_status || null,
    lock_owner_type: lock.lock_owner_type || null
  };
}

async function assertComposerLocked(page, scenario, observer, source) {
  const uiTimeoutMs = Number((scenario.timeouts && scenario.timeouts.aiInputLockUiMs) || 5000);
  await page.evaluate(() => {
    if (typeof runStudentSync === 'function') return runStudentSync();
    return null;
  }).catch(() => null);

  const selectors = {
    input: scenario.selectors.discussionInput,
    send: scenario.selectors.sendButton,
    help: scenario.selectors.helpButton || '.help-btn',
    hint: scenario.selectors.aiLockHint || '#aiLockHint'
  };

  const ok = await page.waitForFunction((s) => {
    const input = document.querySelector(s.input);
    const send = document.querySelector(s.send);
    const help = document.querySelector(s.help);
    const inputLocked = !!input && (input.readOnly || input.disabled || input.getAttribute('aria-disabled') === 'true');
    const sendLocked = !send || send.disabled;
    const helpLocked = !help || help.disabled;
    return inputLocked && sendLocked && helpLocked;
  }, selectors, { timeout: uiTimeoutMs }).then(() => true).catch(() => false);

  const details = await page.evaluate((s) => {
    const input = document.querySelector(s.input);
    const send = document.querySelector(s.send);
    const help = document.querySelector(s.help);
    const hint = document.querySelector(s.hint);
    return {
      inputReadOnly: input ? Boolean(input.readOnly) : null,
      inputDisabled: input ? Boolean(input.disabled) : null,
      inputAriaDisabled: input ? input.getAttribute('aria-disabled') : null,
      sendDisabled: send ? Boolean(send.disabled) : null,
      helpDisabled: help ? Boolean(help.disabled) : null,
      hintVisible: hint ? !hint.hidden : null
    };
  }, selectors).catch(() => ({}));

  if (ok) {
    recordAiLockMetric(observer, 'recordAiInputLockUiOk', { source, details: JSON.stringify(details) });
    return true;
  }

  const authoritativeLock = await getAiLockState(page, scenario).catch(() => ({ locked: true }));
  if (!authoritativeLock.locked) {
    recordAiLockMetric(observer, 'recordLockObserverRace', {
      source,
      phase: 'ui_lock_assertion',
      ui_sync_delay_ms: 0
    });
    return true;
  }

  recordAiLockMetric(observer, 'recordAiInputLockViolation', {
    source,
    reason: 'ui_not_locked',
    details: JSON.stringify(details)
  });
  return false;
}

async function assertAiLockApiRejected(page, scenario, message, observer, source) {
  const groupId = await resolveGroupId(page, {});
  const lockBeforeProbe = await getAiLockState(page, scenario).catch(() => ({ locked: true }));
  if (!lockBeforeProbe.locked) {
    recordAiLockMetric(observer, 'recordLockObserverRace', {
      source,
      phase: 'api_lock_probe',
      ui_sync_delay_ms: 0
    });
    return true;
  }
  const payload = buildAiLockProbePayload(groupId);
  const result = await page.evaluate(async ({ payload, timeoutMs }) => {
    const token = new URLSearchParams(window.location.search).get('tab_token') || '';
    const controller = new AbortController();
    const tid = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const headers = { 'Content-Type': 'application/json', Accept: 'application/json' };
      if (token) headers['X-Tab-Token'] = token;
      const response = await fetch('/api/message', {
        method: 'POST',
        headers,
        body: JSON.stringify(payload),
        signal: controller.signal
      });
      const text = await response.text();
      let data = null;
      try {
        data = text ? JSON.parse(text) : null;
      } catch (_err) {
        data = { raw: text };
      }
      return { status: response.status, ok: response.ok, data };
    } finally {
      clearTimeout(tid);
    }
  }, {
    payload,
    timeoutMs: (scenario.timeouts && scenario.timeouts.apiResponseMs) || 20000
  }).catch((error) => ({ status: 0, ok: false, data: { error: error.message } }));

  if (result.status === 423 && result.data && result.data.code === 'ROOM_AI_INTERVENING') {
    recordAiLockMetric(observer, 'recordAiInputLockApiRejected', {
      source,
      status: result.status,
      reason: result.data.code
    });
    return true;
  }

  const lockAfterProbe = await getAiLockState(page, scenario).catch(() => ({ locked: true }));
  if (!lockAfterProbe.locked) {
    recordAiLockMetric(observer, 'recordLockObserverRace', {
      source,
      phase: 'api_lock_probe',
      ui_sync_delay_ms: 0
    });
    return true;
  }

  recordAiLockMetric(observer, 'recordAiInputLockViolation', {
    source,
    status: result.status,
    reason: 'api_not_rejected',
    details: JSON.stringify(result.data || {})
  });
  return false;
}

function buildAiLockProbePayload(groupId) {
  return {
    group_id: Number(groupId),
    // The API checks the room lease before validating content. An empty probe
    // therefore still proves a 423 lock response, but can never become a real
    // student message if the lease is released between the sync and POST.
    content: '',
    client_message_id: `ai-lock-probe-${Date.now()}-${Math.random().toString(16).slice(2)}`
  };
}

async function waitForAiUnlock(page, scenario, observer, source) {
  const startedAt = Date.now();
  const timeoutMs = Number((scenario.timeouts && scenario.timeouts.aiInputLockMaxWaitMs) || 90000);
  const pollMs = Number((scenario.flow && scenario.flow.aiInputLockPollMs) || 1000);

  while (Date.now() - startedAt < timeoutMs) {
    const lock = await getAiLockState(page, scenario).catch(() => ({ locked: true }));
    if (!lock.locked) {
      const waitMs = Date.now() - startedAt;
      recordAiLockMetric(observer, 'recordAiInputLockWait', { source, waitMs });
      await page.evaluate(() => {
        if (typeof runStudentSync === 'function') return runStudentSync();
        return null;
      }).catch(() => null);
      return { serverUnlocked: true, waitMs };
    }
    await sleep(pollMs);
  }

  const waitMs = Date.now() - startedAt;
  recordAiLockMetric(observer, 'recordAiInputLockTimeout', { source, waitMs });
  throw new Error(`AI input lock did not release within ${timeoutMs}ms`);
}

function agentMessageAfter(syncBody, afterMessageId) {
  return agentMessagesAfter(syncBody, afterMessageId)[0] || null;
}

function agentMessagesAfter(syncBody, afterMessageId) {
  const messages = (syncBody && (
    (syncBody.chat && syncBody.chat.messages) || syncBody.messages
  )) || [];
  return messages.filter((item) => {
    const role = String(item.resolved_role || item.role || item.sender_type || '').toLowerCase();
    return role === 'agent' && Number(item.id || item.message_id || 0) > Number(afterMessageId || 0);
  });
}

function messagePipelineRunId(message) {
  if (!message || typeof message !== 'object') return null;
  const direct = message.strategy_pipeline_run_id || message.pipeline_run_id;
  if (direct !== undefined && direct !== null && String(direct) !== '') return String(direct);
  let metadata = message.metadata_json || message.metadata || null;
  if (typeof metadata === 'string') {
    try { metadata = JSON.parse(metadata); } catch (_error) { metadata = null; }
  }
  if (!metadata || typeof metadata !== 'object') return null;
  const value = metadata.strategy_pipeline_run_id || metadata.pipeline_run_id;
  return value !== undefined && value !== null && String(value) !== '' ? String(value) : null;
}

async function readInterventionBoundary(page, scenario, afterMessageId) {
  const query = new URLSearchParams({
    after_message_id: String(Math.max(0, Number(afterMessageId || 0))),
    limit: '120'
  });
  const response = await browserFetchJson(page, `/api/student/sync?${query.toString()}`, {
    timeoutMs: (scenario.timeouts && scenario.timeouts.apiResponseMs) || 20000
  });
  const body = response.data || {};
  const lock = body.ai_lock || {};
  const room = body.room || {};
  const pipelineRuns = pipelineCandidates(body);
  const firstPipeline = selectPipeline(pipelineRuns);
  const messages = agentMessagesAfter(body, afterMessageId);
  return {
    body,
    locked: Boolean(lock.locked || room.state === 'AI_INTERVENING'),
    reason: lock.reason || (room.state === 'AI_INTERVENING' ? 'ROOM_AI_INTERVENING' : null),
    activeInterventionRunId: lock.active_intervention_run_id || room.active_intervention_run_id || null,
    lockOwnerPipelineRunId: lock.lock_owner_run_id || null,
    lockOwnerStatus: lock.lock_owner_status || null,
    pipelineRuns,
    pipeline: firstPipeline,
    agentMessages: messages,
    agentMessage: messages[0] || null
  };
}

async function waitForExpectedIntervention(page, scenario, observer, options = {}) {
  const afterMessageId = Number(options.afterMessageId || 0);
  if (!afterMessageId) throw new Error('Expected intervention gate requires the trigger message ID.');
  const source = `expected_intervention:${options.scenarioId || 'unknown'}`;
  const startedAt = Date.now();
  const minimumPauseMs = Math.max(0, Number(options.minimumPauseSeconds || 0) * 1000);
  const timeoutMs = Math.max(
    minimumPauseMs + 1000,
    Number((scenario.flow && scenario.flow.expectedInterventionMaxWaitMs) || 5 * 60 * 1000)
  );
  const pollMs = Math.max(250, Number((scenario.flow && scenario.flow.aiInputLockPollMs) || 1000));
  const pipelinePollMs = Math.max(
    50,
    Number((scenario.flow && scenario.flow.expectedPipelinePollMs) || pollMs)
  );
  const pipelineDiscoveryMaxWaitMs = Math.max(
    0,
    Number((scenario.timeouts && scenario.timeouts.expectedPipelineDiscoveryMaxWaitMs) || 10 * 1000)
  );
  let lockObserved = false;
  let agentMessage = null;
  let trackedPipelineRunId = null;
  let pipelineObserved = false;
  const observedLockOwners = new Set();

  recordAiLockMetric(observer, 'recordExpectedInterventionStarted', {
    source,
    scenarioId: options.scenarioId || null,
    messageId: afterMessageId,
    trigger_message_id: afterMessageId,
    pipeline_lookup: 'trigger_message_id'
  });

  while (Date.now() - startedAt < timeoutMs) {
    const boundary = await readInterventionBoundary(page, scenario, afterMessageId);
    const candidatePipeline = selectPipeline(boundary.pipelineRuns, trackedPipelineRunId);
    if (!trackedPipelineRunId && candidatePipeline) {
      trackedPipelineRunId = pipelineRunId(candidatePipeline);
    }
    const pipeline = trackedPipelineRunId
      ? boundary.pipelineRuns.find((item) => pipelineRunId(item) === trackedPipelineRunId) || null
      : candidatePipeline;
    if (pipeline) pipelineObserved = true;

    const messages = boundary.agentMessages || [];
    const matchingMessage = trackedPipelineRunId
      ? messages.find((item) => messagePipelineRunId(item) === trackedPipelineRunId)
      : messages[0];
    if (matchingMessage) agentMessage = matchingMessage;

    const lockOwnerId = boundary.lockOwnerPipelineRunId || boundary.activeInterventionRunId || null;
    const newLockOwner = lockOwnerId !== null && lockOwnerId !== undefined &&
      !observedLockOwners.has(String(lockOwnerId));
    if (boundary.locked && (!lockObserved || newLockOwner)) {
      lockObserved = true;
      if (lockOwnerId !== null && lockOwnerId !== undefined) {
        observedLockOwners.add(String(lockOwnerId));
      }
      recordAiLockMetric(observer, 'recordAiInputLockObserved', {
        source,
        locked: true,
        reason: boundary.reason,
        activeInterventionRunId: boundary.activeInterventionRunId,
        lock_owner_pipeline_run_id: boundary.lockOwnerPipelineRunId
      });
      if (observedLockOwners.size <= 1) {
        const uiLocked = await assertComposerLocked(page, scenario, observer, source);
        if (!uiLocked) throw new Error(`${source} did not lock the student composer.`);
        if (scenario.flow && scenario.flow.verifyAiInputLockApiReject !== false) {
          const apiRejected = await assertAiLockApiRejected(
            page,
            scenario,
            { content: `[EXPECTED_INTERVENTION_PROBE:${options.scenarioId || 'unknown'}]` },
            observer,
            source
          );
          if (!apiRejected) throw new Error(`${source} did not reject the message API with ROOM_AI_INTERVENING.`);
        }
      }
    }

    const terminal = pipelineTerminal(pipeline);
    if (terminal.isTerminal) {
      const timing = pipelineTiming(pipeline, null, boundary.pipelineRuns);
      if (!boundary.locked && lockObserved) {
        await observeClientInputAfterServerRelease(page, scenario, observer, source, timing);
      }
      const remainingPauseMs = minimumPauseMs - (Date.now() - startedAt);
      if (remainingPauseMs > 0) await sleep(remainingPauseMs);
      const waitMs = Date.now() - startedAt;
      if (agentMessage && !lockObserved) {
        recordAiLockMetric(observer, 'recordLockObserverRace', {
          source,
          scenarioId: options.scenarioId || null,
          messageId: afterMessageId,
          agentMessageId: agentMessage.id || agentMessage.message_id || null,
          pipeline_run_id: pipelineRunId(pipeline),
          reason: 'message_visible_without_lock_observation'
        });
      }
      recordAiLockMetric(observer, 'recordExpectedInterventionCompleted', {
        source,
        scenarioId: options.scenarioId || null,
        messageId: afterMessageId,
        trigger_message_id: afterMessageId,
        agentMessageId: agentMessage && (agentMessage.id || agentMessage.message_id) || null,
        pipeline_run_id: pipelineRunId(pipeline),
        pipelineRunId: pipelineRunId(pipeline),
        activeInterventionRunId: boundary.activeInterventionRunId,
        lockObserved,
        lockOwnerPipelineRunId: boundary.lockOwnerPipelineRunId,
        sequential_lease_count: Math.max(1, observedLockOwners.size),
        pipeline_found: true,
        terminal_reason: terminal.terminalReason,
        terminal_status: terminal.terminalStatus,
        published_message_id: pipeline && pipeline.published_message_id || null,
        expected_gate_wait_duration: waitMs,
        waitMs,
        ...timing
      });
      await page.evaluate(() => {
        if (typeof runStudentSync === 'function') return runStudentSync();
        return null;
      }).catch(() => null);
      return {
        lockObserved,
        agentMessageId: agentMessage && (agentMessage.id || agentMessage.message_id) || null,
        pipelineRunId: pipelineRunId(pipeline),
        terminalReason: terminal.terminalReason,
        terminalStatus: terminal.terminalStatus,
        publishedMessageId: pipeline && pipeline.published_message_id || null,
        agentPublishedAtMs: terminal.terminalReason === 'PUBLISHED'
          ? (Date.parse(String(pipeline.published_at || '')) || agentMessageTimestampMs(agentMessage) || Date.now())
          : null,
        pipelineTiming: timing,
        waitMs
      };
    }

    if (!pipelineObserved && Date.now() - startedAt >= pipelineDiscoveryMaxWaitMs) {
      const waitMs = Date.now() - startedAt;
      recordAiLockMetric(observer, 'recordExpectedInterventionFailed', {
        source,
        scenarioId: options.scenarioId || null,
        messageId: afterMessageId,
        trigger_message_id: afterMessageId,
        pipeline_found: false,
        lockObserved,
        waitMs,
        reason: 'pipeline_not_found'
      });
      const error = new Error(
        `${source} did not find a pipeline for trigger message ${afterMessageId} ` +
        `within ${pipelineDiscoveryMaxWaitMs}ms.`
      );
      error.expectedGateReason = 'pipeline_not_found';
      throw error;
    }
    await sleep(pipelineObserved ? pollMs : pipelinePollMs);
  }

  const waitMs = Date.now() - startedAt;
  recordAiLockMetric(observer, 'recordAiInputLockTimeout', { source, waitMs });
  recordAiLockMetric(observer, 'recordExpectedInterventionFailed', {
    source,
    scenarioId: options.scenarioId || null,
    messageId: afterMessageId,
    trigger_message_id: afterMessageId,
    pipeline_run_id: trackedPipelineRunId,
    pipeline_found: pipelineObserved,
    lockObserved,
    agentMessageId: agentMessage && (agentMessage.id || agentMessage.message_id),
    waitMs,
    reason: !pipelineObserved ? 'pipeline_not_found' : 'pipeline_terminal_not_observed'
  });
  throw new Error(
    `${source} did not complete within ${timeoutMs}ms ` +
    `(pipelineObserved=${pipelineObserved}, lockObserved=${lockObserved}, ` +
    `agentMessage=${Boolean(agentMessage)}).`
  );
}

async function observeClientInputAfterServerRelease(page, scenario, observer, source, timing = {}) {
  const graceMs = Math.max(
    0,
    Number((scenario.timeouts && scenario.timeouts.aiInputLockObserverGraceMs) || 5000)
  );
  const pollMs = Math.max(
    25,
    Number((scenario.flow && scenario.flow.messageInputPollMs) || 250)
  );
  const startedAt = Date.now();
  let initialLocked = false;

  while (Date.now() - startedAt <= graceMs) {
    const composer = await readComposerState(page, scenario);
    if (!composer.available || !isComposerLocked(composer)) {
      if (initialLocked || Date.now() > startedAt) {
        const restoredAt = new Date().toISOString();
        recordAiLockMetric(observer, 'recordLockObserverRace', {
          source,
          phase: 'ui_sync_after_server_release',
          reason: 'LOCK_OBSERVER_RACE',
          server_lease_released_at: timing.lease_released_at || null,
          client_input_restored_at: restoredAt,
          ui_sync_delay_ms: Date.now() - startedAt
        });
        recordAiLockMetric(observer, 'recordAiInputLockRestored', {
          source,
          client_input_restored_at: restoredAt
        });
      }
      return true;
    }
    initialLocked = true;
    await sleep(Math.min(pollMs, Math.max(1, graceMs - (Date.now() - startedAt))));
  }

  if (initialLocked) {
    recordAiLockMetric(observer, 'recordLockObserverRace', {
      source,
      phase: 'ui_sync_grace_exhausted',
      reason: 'LOCK_OBSERVER_RACE',
      server_lease_released_at: timing.lease_released_at || null,
      ui_sync_delay_ms: Date.now() - startedAt
    });
  }
  return false;
}

function agentMessageTimestampMs(message) {
  if (!message || typeof message !== 'object') return null;
  for (const value of [message.created_at, message.createdAt, message.sent_at, message.sentAt]) {
    if (!value) continue;
    const timestamp = Date.parse(value);
    if (Number.isFinite(timestamp)) return timestamp;
  }
  return null;
}

async function verifyNoIntervention(page, scenario, observer, options = {}) {
  const afterMessageId = Number(options.afterMessageId || 0);
  if (!afterMessageId) throw new Error('No-intervention check requires the final student message ID.');
  const source = `expected_restraint:${options.scenarioId || 'unknown'}`;
  const observationMs = Math.max(1000, Number(options.observationSeconds || 30) * 1000);
  const timeoutMs = Math.max(
    observationMs,
    Number((scenario.flow && scenario.flow.expectedInterventionMaxWaitMs) || 5 * 60 * 1000)
  );
  const pollMs = Math.max(
    250,
    Number((scenario.flow && scenario.flow.expectedNoInterventionPollMs) || 1000)
  );
  const startedAt = Date.now();
  let lockObserved = false;
  let lockObservedAt = null;
  let lastBoundary = null;
  recordAiLockMetric(observer, 'recordExpectedNoInterventionStarted', {
    source,
    scenarioId: options.scenarioId || null,
    messageId: afterMessageId
  });

  while (Date.now() - startedAt < timeoutMs) {
    const boundary = await readInterventionBoundary(page, scenario, afterMessageId);
    lastBoundary = boundary;
    if (boundary.agentMessage) {
      const reason = 'unexpected_agent_message';
      recordAiLockMetric(observer, 'recordExpectedNoInterventionFailed', {
        source,
        scenarioId: options.scenarioId || null,
        messageId: afterMessageId,
        agentMessageId: boundary.agentMessage && (
          boundary.agentMessage.id || boundary.agentMessage.message_id
        ),
        reason
      });
      throw new Error(`${source} failed: ${reason}.`);
    }
    if (boundary.locked && !lockObserved) {
      lockObserved = true;
      lockObservedAt = Date.now();
      recordAiLockMetric(observer, 'recordAiInputLockObserved', {
        source,
        locked: true,
        reason: boundary.reason,
        activeInterventionRunId: boundary.activeInterventionRunId,
        lock_owner_pipeline_run_id: boundary.lockOwnerPipelineRunId
      });
    }
    const elapsedMs = Date.now() - startedAt;
    if (!boundary.locked && elapsedMs >= observationMs) {
      const waitMs = lockObservedAt ? Date.now() - lockObservedAt : 0;
      if (lockObserved) {
        recordAiLockMetric(observer, 'recordAiInputLockWait', { source, waitMs });
      }
      recordAiLockMetric(observer, 'recordExpectedNoInterventionCompleted', {
        source,
        scenarioId: options.scenarioId || null,
        messageId: afterMessageId,
        lockObserved,
        waitMs: Date.now() - startedAt
      });
      return true;
    }
    await sleep(Math.min(pollMs, Math.max(1, timeoutMs - elapsedMs)));
  }

  const reason = lastBoundary && lastBoundary.locked
    ? 'room_lock_timeout'
    : 'observation_timeout';
  recordAiLockMetric(observer, 'recordExpectedNoInterventionFailed', {
    source,
    scenarioId: options.scenarioId || null,
    messageId: afterMessageId,
    agentMessageId: null,
    reason
  });
  throw new Error(`${source} failed: ${reason}.`);
}

async function waitForMessageVisible(page, scenario, visibleText, beforeCount) {
  const markerText = String(visibleText || '').trim();
  if (!markerText) return;
  await page.waitForFunction(({ chatBoxSelector, messageTextSelector, markerText, beforeCount }) => {
    const root = document.querySelector(chatBoxSelector) || document.body;
    const count = messageTextSelector
      ? document.querySelectorAll(messageTextSelector).length
      : null;
    const countAdvanced = beforeCount === null || beforeCount === undefined || count === null || count > beforeCount;
    return countAdvanced && root && root.innerText && root.innerText.includes(markerText);
  }, {
    chatBoxSelector: scenario.selectors.chatBox,
    messageTextSelector: scenario.selectors.messageText,
    beforeCount,
    markerText
  }, {
    timeout: scenario.timeouts.messageVisibleMs
  });
}

async function maybeScroll(page, scenario, profile) {
  if (!chance(profile.scrollChance || 0)) return false;

  await page.evaluate((chatBoxSelector) => {
    const box = document.querySelector(chatBoxSelector);
    if (box) {
      const delta = (Math.random() - 0.25) * box.clientHeight * 1.6;
      box.scrollTop = Math.max(0, Math.min(box.scrollHeight, box.scrollTop + delta));
      return;
    }
    window.scrollBy(0, (Math.random() - 0.25) * window.innerHeight);
  }, scenario.selectors.chatBox);

  return true;
}

async function buildReplyLikeMessage(page, scenario, baseMessage) {
  const recentMessages = await page.locator(scenario.selectors.messageText).allTextContents().catch(() => []);
  const cleanRecent = recentMessages
    .map((text) => stripLoadMarkers(text).trim())
    .filter((text) => text && !text.includes(baseMessage.marker))
    .slice(-8);

  const quoted = cleanRecent.length ? pickRandom(cleanRecent).slice(0, 80) : 'the previous point';
  const content = `Replying to: ${quoted}. I want to add one more reason.`;
  return {
    ...baseMessage,
    content,
    visibleText: content
  };
}

function buildMessage({ runId, studentId, seq, scenario, kind }) {
  const marker = `[LOAD_TEST:${runId}:${studentId}:${seq}:${kind}]`;
  const content = pickRandom(scenario.messageTexts);
  return {
    marker,
    content,
    visibleText: content
  };
}

function stripLoadMarkers(text) {
  return String(text || '').replace(/\[(?:LOAD_TEST|LOAD_SCRIPT):[^\]]+\]\s*/g, '');
}

async function browserFetchJson(page, url, options = {}) {
  const {
    method = 'GET',
    body,
    timeoutMs = 20000,
    okStatuses = [200]
  } = options;

  return page.evaluate(async ({ url, method, body, timeoutMs, okStatuses }) => {
    const token = new URLSearchParams(window.location.search).get('tab_token') || '';
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    const headers = { Accept: 'application/json' };
    if (token) headers['X-Tab-Token'] = token;
    if (body !== undefined) headers['Content-Type'] = 'application/json';

    try {
      const response = await fetch(url, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal
      });
      const text = await response.text();
      let data = null;
      try {
        data = text ? JSON.parse(text) : null;
      } catch (_err) {
        data = { raw: text };
      }
      if (!okStatuses.includes(response.status)) {
        throw new Error(`HTTP ${response.status}: ${text.slice(0, 500)}`);
      }
      return { status: response.status, data };
    } finally {
      clearTimeout(timeout);
    }
  }, { url, method, body, timeoutMs, okStatuses });
}

async function resolveGroupId(page, student) {
  if (student.group_id) return Number(student.group_id);
  if (student.groupId) return Number(student.groupId);
  const groupId = await page.evaluate(() => (
    typeof GROUP_ID !== 'undefined'
      ? GROUP_ID
      : (typeof window.GROUP_ID !== 'undefined' ? window.GROUP_ID : null)
  ));
  if (!groupId) throw new Error(`group id missing for ${student.id}`);
  return Number(groupId);
}

function buildQuestionnaireResponses(questionnaire, student, stage) {
  const responses = {};
  const items = questionnaire.items || flattenQuestionnaireItems(questionnaire);
  for (const item of items) {
    if (!item || !item.id) continue;
    responses[item.id] = responseForQuestionnaireItem(item, student, stage);
  }
  return responses;
}

function flattenQuestionnaireItems(questionnaire) {
  const items = [];
  for (const section of questionnaire.sections || []) {
    for (const item of section.items || []) items.push(item);
  }
  return items;
}

function responseForQuestionnaireItem(item, student, stage) {
  const type = String(item.question_type || '').toLowerCase();
  if (type.includes('text')) {
    return { text: `${stage} response from ${student.participant_code || student.id}` };
  }

  const options = item.options || [];
  if (options.length || type.includes('choice') || type.includes('situational')) {
    return { option_key: optionKey(options, student, item) };
  }

  const min = Number(item.min_value || 1);
  const max = Number(item.max_value || (type.includes('7') ? 7 : 5));
  return boundedLikertValue(min, max, student, item);
}

function optionKey(options, student, item) {
  if (!options.length) return 'A';
  const selected = options[stableIndex(student, item, options.length)];
  if (typeof selected === 'string') return selected;
  if (selected && typeof selected === 'object') {
    return String(selected.key || selected.value || selected.label || selected.text || 'A');
  }
  return String(selected);
}

function boundedLikertValue(min, max, student, item) {
  const span = Math.max(1, max - min + 1);
  const value = min + stableIndex(student, item, span);
  return Math.max(min, Math.min(max, value));
}

function stableIndex(student, item, modulo) {
  const text = `${student.id}|${student.group_code || ''}|${item.id || ''}|${item.item_code || ''}`;
  let hash = 0;
  for (let i = 0; i < text.length; i += 1) {
    hash = ((hash << 5) - hash + text.charCodeAt(i)) | 0;
  }
  return Math.abs(hash) % Math.max(1, modulo);
}

function buildDeliverableText(student, runId) {
  const group = student.group_code || student.groupCode || `G${student.group_no || ''}`;
  return [
    `[LOAD_TEST_DELIVERABLE:${runId}:${group}]`,
    `Group: ${group}`,
    `Submitted by: ${student.participant_code || student.id}`,
    'Final conclusion: the group compared the candidate options and selected the plan with the best balance of feasibility, cost, and learning value.',
    'Evidence: members raised constraints, clarified disagreement, asked for support when blocked, and refined the final document before submission.',
    'Next step: the group would verify the assumptions and prepare a short presentation of the decision process.'
  ].join('\n');
}

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, (m) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#039;'
  }[m]));
}

function shouldSendMessage(profile) {
  return chance(profile.sendChance === undefined ? 1 : profile.sendChance);
}

function shouldReply(profile) {
  return chance(profile.replyChance || 0);
}

function randomThinkTime(profile) {
  return randomBetween(profile.messageIntervalMs);
}

async function pageDiagnostic(page) {
  return page.evaluate(() => {
    const title = document.title || '';
    const text = (document.body && document.body.innerText ? document.body.innerText : '').replace(/\s+/g, ' ').slice(0, 300);
    return `title=${title}; text=${text}`;
  }).catch((err) => `diagnostic failed: ${err.message}`);
}

module.exports = {
  login,
  browserFetchJson,
  enterDiscussion,
  waitForDiscussionReady,
  submitQuestionnaires,
  submitCheckin,
  requestStudentHelp,
  submitPostEmotionCheckin,
  fetchGroupTranscript,
  submitGroupDeliverable,
  sendDiscussionMessage,
  waitForComposerEditable,
  readComposerState,
  classifyMessageSendFailure,
  MESSAGE_SEND_STATUS,
  buildAiLockProbePayload,
  waitForExpectedIntervention,
  verifyNoIntervention,
  maybeScroll,
  buildReplyLikeMessage,
  buildMessage,
  shouldSendMessage,
  shouldReply,
  randomThinkTime,
  pageDiagnostic
};
