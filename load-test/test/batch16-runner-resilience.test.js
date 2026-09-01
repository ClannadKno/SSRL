const assert = require('node:assert/strict');
const test = require('node:test');

const { login } = require('../src/actions');
const { RunState } = require('../src/runState');
const { isBrowserPageCrash, openLoggedInStudentPage } = require('../src/student');

function loginScenario() {
  return {
    baseUrl: 'http://server.test',
    loginPath: '/login',
    selectors: {
      loginKeyInput: 'input[name="login_key"]',
      loginSubmit: 'button[type="submit"]'
    },
    timeouts: {
      navigationMs: 1000,
      loginMs: 3000
    },
    flow: {
      loginAttempts: 3,
      loginRetryDelayMs: 0
    }
  };
}

function response(status, location = null) {
  return {
    url: () => 'http://server.test/login',
    request: () => ({ method: () => 'POST' }),
    status: () => status,
    headers: () => location ? { location } : {}
  };
}

function loginPage(responses, { stallRedirect = false } = {}) {
  let currentUrl = 'http://server.test/login';
  let responseIndex = 0;
  const calls = { click: 0, goto: [] };
  const locator = {
    first: () => locator,
    fill: async () => {},
    click: async () => {
      calls.click += 1;
    }
  };
  return {
    calls,
    url: () => currentUrl,
    goto: async (url) => {
      calls.goto.push(url);
      currentUrl = url;
    },
    locator: () => locator,
    waitForResponse: async (predicate) => {
      const item = responses[Math.min(responseIndex, responses.length - 1)];
      responseIndex += 1;
      assert.equal(predicate(item), true);
      return item;
    },
    waitForFunction: async () => {
      const item = responses[Math.max(0, responseIndex - 1)];
      if (item.status() >= 400 || stallRedirect) throw new Error('redirect did not finish');
      currentUrl = 'http://server.test/student/waiting';
    },
    waitForLoadState: async () => {},
    waitForTimeout: async () => {},
    evaluate: async () => 'login page'
  };
}

test('login retries an observed transient server failure within one total budget', async () => {
  const page = loginPage([
    response(500),
    response(302, '/student/waiting')
  ]);

  const result = await login(
    page,
    { id: 'S1-G06-M2', loginKey: 'test-key' },
    loginScenario()
  );

  assert.equal(result.attemptCount, 2);
  assert.equal(result.url, 'http://server.test/student/waiting');
  assert.equal(page.calls.click, 2);
});

test('login follows a successful redirect location when the browser navigation stalls', async () => {
  const page = loginPage([
    response(302, '/student/waiting')
  ], { stallRedirect: true });

  const result = await login(
    page,
    { id: 'S1-G06-M3', loginKey: 'test-key' },
    loginScenario()
  );

  assert.equal(result.attemptCount, 1);
  assert.equal(result.url, 'http://server.test/student/waiting');
  assert.deepEqual(page.calls.goto, [
    'http://server.test/login',
    'http://server.test/student/waiting'
  ]);
});

test('a crashed login page is replaced with a fresh browser context', async () => {
  const closed = [];
  const pages = [{ id: 'crashed-page' }, { id: 'fresh-page' }];
  let contextIndex = 0;
  const events = [];
  const result = await openLoggedInStudentPage({
    browser: {},
    student: { id: 'S1-G01-M2', loginKey: 'test-key' },
    scenario: { flow: { loginContextAttempts: 2 } },
    metrics: { event: (type, payload) => events.push({ type, payload }) },
    contextFactory: async () => {
      const index = contextIndex;
      contextIndex += 1;
      return {
        newPage: async () => pages[index],
        close: async () => closed.push(index)
      };
    },
    bindMetrics: () => {},
    loginFn: async (page) => {
      if (page === pages[0]) throw new Error('page.goto: Page crash');
      return { latencyMs: 12, url: 'http://server.test/student/waiting' };
    }
  });

  assert.equal(result.page, pages[1]);
  assert.equal(result.contextRecoveryCount, 1);
  assert.deepEqual(closed, [0]);
  assert.equal(events[0].type, 'login_context_recreated');
  assert.equal(isBrowserPageCrash(new Error('page.goto: Page crash')), true);
  assert.equal(isBrowserPageCrash(new Error('HTTP 500')), false);
});

test('abandoning a failed participant releases later same-group turns and fails barriers', async () => {
  const runState = new RunState({
    runId: 'batch16-abandon',
    totalStudents: 3,
    minReadyStudents: 3,
    scriptedMessages: [
      { groupCode: 'G04', seq: 1, studentId: 'S1-G04-M1' },
      { groupCode: 'G04', seq: 2, studentId: 'S1-G04-M2' },
      { groupCode: 'G04', seq: 3, studentId: 'S1-G04-M3' }
    ]
  });

  const second = runState.waitForScriptedMessageTurn('G04', 2);
  const groupBarrier = runState.waitForScriptedMessagesComplete('G04');
  const globalBarrier = runState.waitForAllScriptedMessagesComplete();
  const abandoned = runState.abandonScriptedMessagesForStudent('S1-G04-M1');

  assert.deepEqual(abandoned, [{ groupCode: 'G04', sequence: 1 }]);
  assert.equal(await second, true);
  runState.completeScriptedMessageTurn('G04', 2);
  runState.completeScriptedMessageTurn('G04', 3);
  assert.equal(await groupBarrier, false);
  assert.equal(await globalBarrier, false);
  assert.equal(await runState.waitForScriptedMessageTurn('G04', 1), false);
});
