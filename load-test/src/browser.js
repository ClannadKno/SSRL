const { chromium } = require('playwright');

async function launchSharedBrowser(scenario) {
  return chromium.launch({
    headless: scenario.browser.headless,
    slowMo: scenario.browser.slowMo,
    args: scenario.browser.launchArgs
  });
}

async function createStudentContext(browser, scenario, student) {
  const context = await browser.newContext({
    ...scenario.context,
    userAgent: `SSRL-ESP-DiscussionLoadTest/${student.id}`
  });

  await applyResourceMode(context, scenario);
  return context;
}

async function applyResourceMode(context, scenario) {
  const mode = scenario.resourceBlocking[scenario.resourceMode] || scenario.resourceBlocking.light;
  const blockTypes = new Set(mode.blockResourceTypes || []);
  const blockPatterns = mode.blockUrlPatterns || [];

  if (!blockTypes.size && !blockPatterns.length) return;

  await context.route('**/*', async (route) => {
    const request = route.request();
    const url = request.url();
    const type = request.resourceType();
    const shouldBlockType = blockTypes.has(type);
    const shouldBlockUrl = blockPatterns.some((pattern) => url.includes(pattern));
    if (shouldBlockType || shouldBlockUrl) {
      await route.abort();
      return;
    }
    await route.continue();
  });
}

module.exports = {
  launchSharedBrowser,
  createStudentContext
};
