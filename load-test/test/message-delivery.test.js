const assert = require('node:assert/strict');
const test = require('node:test');

const {
  MESSAGE_SEND_STATUS,
  beginMessageDeliveryAttempt,
  buildClientMessageId,
  classifyMessageSendFailure,
  completeMessageDeliveryAttempt,
  createMessageDeliveryRecord,
  finalizeMessageDelivery,
  isComposerLocked
} = require('../src/messageDelivery');
const {
  sendDiscussionMessage,
  waitForComposerEditable
} = require('../src/actions');
const {
  buildActualServerCoverage,
  buildScriptedMessageInputIntegrity
} = require('../src/serverCoverage');

function scenario(overrides = {}) {
  return {
    selectors: {
      discussionInput: '#messageInput',
      sendButton: '.send-btn',
      messageText: '#chatBox .msg-text'
    },
    timeouts: {
      apiResponseMs: 1000,
      messageVisibleMs: 1000,
      messageInputMaxWaitMs: 100
    },
    flow: {
      verifyAiInputLock: false,
      verifyAiInputLockApiReject: false,
      aiInputLockSendRetries: 1,
      messageInputPollMs: 1,
      messageInputMaxPollMs: 2,
      messageRetryBaseMs: 0,
      messageRetryMaxMs: 0
    },
    ...overrides
  };
}

test('composer lock detection covers readonly, aria-disabled, and ai-locked class', () => {
  assert.equal(isComposerLocked({ readOnly: true }), true);
  assert.equal(isComposerLocked({ ariaDisabled: 'true' }), true);
  assert.equal(isComposerLocked({ className: 'chat-input ai-locked' }), true);
  assert.equal(isComposerLocked({ className: 'chat-input', disabled: false }), false);
});

test('composer waits through an initial lock and bounded polling restores input', async () => {
  const states = [
    { available: true, locked: true, readOnly: true, className: 'ai-locked' },
    { available: true, locked: false, readOnly: false, className: '' }
  ];
  const waits = [];
  const page = {
    evaluate: async (fn) => {
      const source = fn.toString();
      if (source.includes('input.readOnly')) return states.shift() || states[states.length - 1];
      return null;
    }
  };

  const waited = await waitForComposerEditable(
    page,
    scenario(),
    { metrics: { recordAiInputLockWait: (data) => waits.push(data) } },
    'test_initial_lock'
  );

  assert.ok(waited >= 0);
  assert.equal(waits.length, 1);
});

test('message send failures retain distinct delivery phases', () => {
  assert.equal(classifyMessageSendFailure({ status: 423 }), MESSAGE_SEND_STATUS.HTTP_423_LOCKED);
  assert.equal(
    classifyMessageSendFailure(new Error('fill timed out'), { stage: 'fill' }),
    MESSAGE_SEND_STATUS.UI_FILL_FAILED
  );
  assert.equal(
    classifyMessageSendFailure(new Error('composer locked'), { stage: 'input_locked' }),
    MESSAGE_SEND_STATUS.UI_INPUT_LOCKED
  );
  assert.equal(
    classifyMessageSendFailure(new Error('visibility timeout'), { stage: 'visible' }),
    MESSAGE_SEND_STATUS.MESSAGE_NOT_VISIBLE
  );
  assert.equal(classifyMessageSendFailure({ status: 404 }), MESSAGE_SEND_STATUS.DATABASE_NOT_FOUND);
  assert.equal(classifyMessageSendFailure({ status: 500 }), MESSAGE_SEND_STATUS.HTTP_OTHER_ERROR);
});

test('HTTP 423 retries the same message index and client ID without duplicating a student message', async () => {
  const postCalls = [];
  const deliveryAttempts = [];
  let postCount = 0;
  const page = {
    locator: () => ({
      count: async () => 0,
      first: () => ({
        fill: async () => {}
      })
    }),
    waitForFunction: async () => {},
    evaluate: async (fn, arg) => {
      const source = fn.toString();
      if (source.includes("fetch('/api/message'")) {
        postCalls.push(arg.clientMessageId);
        postCount += 1;
        return postCount === 1
          ? { status: 423, ok: false, body: { code: 'ROOM_AI_INTERVENING' } }
          : { status: 200, ok: true, body: { ok: true, message_id: 77 } };
      }
      if (source.includes('input.readOnly')) {
        return { available: true, locked: false, readOnly: false, className: '' };
      }
      if (source.includes('/api/student/sync')) {
        return {
          status: 200,
          data: { ai_lock: { locked: false }, room: { state: 'DISCUSSING' } }
        };
      }
      if (source.includes('typeof GROUP_ID')) return 9;
      return null;
    }
  };
  const metrics = {
    recordMessageDeliveryAttempt: (data) => deliveryAttempts.push(data),
    recordMessageDeliveryFinal: () => {},
    counters: { messageDeliveryAttempted: 0 },
    recordAiInputLockWait: () => {}
  };
  const message = {
    content: 'retry me',
    visibleText: 'retry me',
    messageIndex: 12,
    clientMessageId: buildClientMessageId({
      runId: 'run-1',
      studentId: 'S1-G01-M1',
      messageIndex: 12,
      kind: 'script:frustration'
    })
  };

  const result = await sendDiscussionMessage(page, scenario(), message, {
    metrics,
    studentId: 'S1-G01-M1',
    groupCode: 'G01'
  });

  assert.equal(result.messageId, 77);
  assert.deepEqual(postCalls, [message.clientMessageId, message.clientMessageId]);
  assert.deepEqual(deliveryAttempts.map((item) => item.status), [
    MESSAGE_SEND_STATUS.HTTP_423_LOCKED,
    MESSAGE_SEND_STATUS.SUCCESS
  ]);
  assert.equal(result.delivery.message_index, 12);
  assert.equal(result.delivery.client_message_id, message.clientMessageId);
  assert.equal(result.delivery.attempt_count, 2);
});

test('maximum input-lock wait terminates with UI_INPUT_LOCKED', async () => {
  const page = {
    evaluate: async (fn) => {
      if (fn.toString().includes('input.readOnly')) {
        return { available: true, locked: true, readOnly: true, className: 'ai-locked' };
      }
      return null;
    }
  };

  await assert.rejects(
    waitForComposerEditable(
      page,
      scenario({ timeouts: { messageInputMaxWaitMs: 8 } }),
      { metrics: {} },
      'test_timeout'
    ),
    (error) => error.deliveryStatus === MESSAGE_SEND_STATUS.UI_INPUT_LOCKED
  );
});

test('delivery records preserve all attempts and timestamps for one logical message', () => {
  const times = ['2026-08-01T00:00:00.000Z', '2026-08-01T00:00:01.000Z', '2026-08-01T00:00:02.000Z'];
  const record = createMessageDeliveryRecord({
    messageIndex: 3,
    clientMessageId: 'client-3'
  }, () => times.shift());
  const first = beginMessageDeliveryAttempt(record);
  completeMessageDeliveryAttempt(record, first, MESSAGE_SEND_STATUS.HTTP_423_LOCKED, { http_status: 423 });
  const second = beginMessageDeliveryAttempt(record);
  completeMessageDeliveryAttempt(record, second, MESSAGE_SEND_STATUS.SUCCESS, { message_id: 3 });
  const final = finalizeMessageDelivery(record, MESSAGE_SEND_STATUS.SUCCESS);

  assert.equal(final.message_index, 3);
  assert.equal(final.client_message_id, 'client-3');
  assert.equal(final.attempt_count, 2);
  assert.equal(final.first_attempt_at, '2026-08-01T00:00:00.000Z');
  assert.equal(final.final_status, MESSAGE_SEND_STATUS.SUCCESS);
  assert.deepEqual(final.attempts.map((attempt) => attempt.status), [
    MESSAGE_SEND_STATUS.HTTP_423_LOCKED,
    MESSAGE_SEND_STATUS.SUCCESS
  ]);
});

test('input completeness gate blocks state accuracy when one scripted message is missing', () => {
  const scenarioConfig = {
    scriptedDiscussion: {
      messages: [
        { studentId: 'S1', seq: 1, scenarioId: 'S1' },
        { studentId: 'S2', seq: 2, scenarioId: 'S2' }
      ],
      scenarios: []
    },
    strategyAudit: { enabled: true }
  };
  const events = [
    { type: 'message_attempt', kind: 'script:state', studentId: 'S1', messageIndex: 1 },
    { type: 'message_attempt', kind: 'script:state', studentId: 'S2', messageIndex: 2 },
    { type: 'message_success', kind: 'script:state', studentId: 'S1', messageIndex: 1, messageId: 11 },
    {
      type: 'message_failure',
      kind: 'script:state',
      scenarioId: 'S2',
      studentId: 'S2',
      messageIndex: 2,
      finalStatus: MESSAGE_SEND_STATUS.UI_INPUT_LOCKED
    }
  ];

  const integrity = buildScriptedMessageInputIntegrity(scenarioConfig, events);
  assert.equal(integrity.status, 'INPUT_INCOMPLETE');
  assert.equal(integrity.expected_script_messages, 2);
  assert.equal(integrity.successful_script_messages, 1);
  assert.deepEqual(integrity.failure_statuses, [MESSAGE_SEND_STATUS.UI_INPUT_LOCKED]);

  const coverage = buildActualServerCoverage(
    scenarioConfig,
    { auditAvailable: true, groups: [] },
    { auditAvailable: true, groups: [] },
    events,
    { inputIntegrity: integrity }
  );
  assert.equal(coverage.validationMode, 'input_incomplete');
  assert.equal(coverage.actualStateCoverage, null);
  assert.equal(coverage.passed, false);
});
