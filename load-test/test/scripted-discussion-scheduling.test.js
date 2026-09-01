const assert = require('node:assert/strict');
const test = require('node:test');

const { verifyNoIntervention, waitForDiscussionReady } = require('../src/actions');
const { canRunScheduledScriptedMessage } = require('../src/student');
const { RunState } = require('../src/runState');

test('drains an in-window scripted message after the nominal end', () => {
  const runState = {
    startedAt: 1_000,
    endAt: 11_000,
    stopRequested: false
  };

  assert.equal(canRunScheduledScriptedMessage(runState, 10_000), true);
});

test('does not drain an out-of-window message or an explicit stop', () => {
  const runState = {
    startedAt: 1_000,
    endAt: 11_000,
    stopRequested: false
  };

  assert.equal(canRunScheduledScriptedMessage(runState, 11_001), false);
  runState.stopRequested = true;
  assert.equal(canRunScheduledScriptedMessage(runState, 10_000), false);
});

test('same-group scripted messages preserve global sequence across students', async () => {
  const runState = new RunState({
    runId: 'ordered-script',
    totalStudents: 2,
    minReadyStudents: 2,
    scriptedMessages: [
      { groupCode: 'G01', seq: 1 },
      { groupCode: 'G01', seq: 2 }
    ]
  });
  const observed = [];
  const second = runState.waitForScriptedMessageTurn('G01', 2).then((allowed) => {
    observed.push(`second:${allowed}`);
  });

  await Promise.resolve();
  assert.deepEqual(observed, []);
  assert.equal(await runState.waitForScriptedMessageTurn('G01', 1), true);
  runState.completeScriptedMessageTurn('G01', 1);
  await second;
  assert.deepEqual(observed, ['second:true']);
});

test('overdue scripted messages preserve real same-group cadence instead of bursting', async () => {
  const runState = new RunState({
    runId: 'paced-script',
    totalStudents: 2,
    minReadyStudents: 2,
    scriptedMessages: [
      { groupCode: 'G01', seq: 1 },
      { groupCode: 'G01', seq: 2 }
    ]
  });

  runState.completeScriptedMessageTurn('G01', 1);
  const startedAt = Date.now();
  assert.equal(await runState.waitForScriptedMessageCadence('G01', 0.03), true);
  assert.ok(Date.now() - startedAt >= 20);
});

test('transcript barrier waits until every same-group scripted turn drains', async () => {
  const runState = new RunState({
    runId: 'transcript-barrier',
    totalStudents: 2,
    minReadyStudents: 2,
    scriptedMessages: [
      { groupCode: 'G01', seq: 1 },
      { groupCode: 'G01', seq: 2 }
    ]
  });

  let released = false;
  const barrier = runState.waitForScriptedMessagesComplete('G01').then((value) => {
    released = value;
  });

  runState.completeScriptedMessageTurn('G01', 1);
  await Promise.resolve();
  assert.equal(released, false);

  runState.completeScriptedMessageTurn('G01', 2);
  await barrier;
  assert.equal(released, true);
});

test('global scripted barrier drains only after every group and can close the window early', async () => {
  const runState = new RunState({
    runId: 'global-script-barrier',
    totalStudents: 4,
    minReadyStudents: 4,
    scriptedMessages: [
      { groupCode: 'G01', seq: 1 },
      { groupCode: 'G02', seq: 2 }
    ]
  });
  runState.start(60_000);

  let released = false;
  const barrier = runState.waitForAllScriptedMessagesComplete().then((value) => {
    released = value;
  });
  runState.completeScriptedMessageTurn('G01', 1);
  await Promise.resolve();
  assert.equal(released, false);

  runState.completeScriptedMessageTurn('G02', 2);
  await barrier;
  assert.equal(released, true);
  assert.equal(runState.shouldStop(), false);
  assert.equal(runState.finishDiscussionWindow(), true);
  assert.equal(runState.shouldStop(), true);
});

test('scripted ordering is isolated by group and stop releases waiters', async () => {
  const runState = new RunState({
    runId: 'isolated-script',
    totalStudents: 4,
    minReadyStudents: 4,
    scriptedMessages: [
      { groupCode: 'G01', seq: 1 },
      { groupCode: 'G01', seq: 3 },
      { groupCode: 'G02', seq: 2 }
    ]
  });

  assert.equal(await runState.waitForScriptedMessageTurn('G02', 2), true);
  const blocked = runState.waitForScriptedMessageTurn('G01', 3);
  runState.requestStop('test stop');
  assert.equal(await blocked, false);
});

test('no-intervention check allows a temporary authoritative assessment lock', async () => {
  let polls = 0;
  const page = {
    evaluate: async () => {
      polls += 1;
      const locked = polls === 1;
      return {
        status: 200,
        data: {
          ai_lock: {
            locked,
            reason: locked ? 'ROOM_AI_INTERVENING' : null,
            active_intervention_run_id: locked ? -201 : null
          },
          room: { state: locked ? 'AI_INTERVENING' : 'DISCUSSING' },
          messages: []
        }
      };
    }
  };
  const events = [];
  const metrics = {};
  for (const method of [
    'recordExpectedNoInterventionStarted',
    'recordExpectedNoInterventionCompleted',
    'recordExpectedNoInterventionFailed',
    'recordAiInputLockObserved',
    'recordAiInputLockWait'
  ]) {
    metrics[method] = (payload) => events.push({ method, payload });
  }
  const result = await verifyNoIntervention(
    page,
    {
      timeouts: { apiResponseMs: 1000 },
      flow: {
        expectedNoInterventionPollMs: 250,
        expectedInterventionMaxWaitMs: 1500
      }
    },
    { metrics, studentId: 'S1-G02-M4', groupCode: 'G02' },
    { afterMessageId: 167, scenarioId: 'S06', observationSeconds: 0.001 }
  );

  assert.equal(result, true);
  assert.ok(polls >= 2);
  assert.equal(events.filter((event) => event.method === 'recordAiInputLockObserved').length, 1);
  assert.equal(events.filter((event) => event.method === 'recordExpectedNoInterventionCompleted').length, 1);
  assert.equal(events.filter((event) => event.method === 'recordExpectedNoInterventionFailed').length, 0);
});

test('discussion ready wait refreshes a stale discussion page once', async () => {
  let waitForFunctionCalls = 0;
  let reloadCalls = 0;
  const page = {
    url: () => 'http://server.test/student/collab?phase=discussion',
    waitForFunction: async () => {
      waitForFunctionCalls += 1;
      if (waitForFunctionCalls === 1) {
        throw new Error('waiting room stayed stale');
      }
    },
    waitForSelector: async () => {},
    evaluate: async () => ({ groupId: 1, mode: 'discussion' }),
    reload: async () => {
      reloadCalls += 1;
    },
    waitForLoadState: async () => {}
  };

  const ready = await waitForDiscussionReady(page, {
    baseUrl: 'http://server.test',
    discussionPath: '/student/collab?phase=discussion',
    selectors: {
      discussionInput: '#messageInput',
      chatBox: '#chatBox'
    },
    timeouts: {
      discussionReadyMs: 1000,
      navigationMs: 1000
    },
    flow: {
      discussionReadyRefreshAttempts: 1,
      discussionReadyRefreshAfterMs: 1
    }
  });

  assert.equal(reloadCalls, 1);
  assert.equal(waitForFunctionCalls, 2);
  assert.deepEqual(ready, {
    groupId: 1,
    mode: 'discussion',
    readyRefreshCount: 1
  });
});
