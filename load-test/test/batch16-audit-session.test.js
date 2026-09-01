const assert = require('node:assert/strict');
const test = require('node:test');

const {
  closeStrategyAuditSession,
  createStrategyAuditSession
} = require('../src/strategyAudit');

test('teacher audit keeps one authenticated context until the run closes it', async () => {
  const page = { id: 'teacher-audit-page' };
  let contextCount = 0;
  let pageCount = 0;
  let loginCount = 0;
  let closeCount = 0;
  const context = {
    newPage: async () => {
      pageCount += 1;
      return page;
    },
    close: async () => {
      closeCount += 1;
    }
  };

  const session = await createStrategyAuditSession({
    browser: {},
    scenario: {},
    auditor: { id: 'teacher', loginKey: 'teacher-key' },
    contextFactory: async () => {
      contextCount += 1;
      return context;
    },
    loginFn: async (actualPage) => {
      loginCount += 1;
      assert.equal(actualPage, page);
    }
  });

  assert.equal(session.page, page);
  assert.equal(contextCount, 1);
  assert.equal(pageCount, 1);
  assert.equal(loginCount, 1);

  await closeStrategyAuditSession(session);
  await closeStrategyAuditSession(session);
  assert.equal(closeCount, 1);
});

