const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');
const { login, browserFetchJson } = require('./actions');
const { buildBatch18LabelAudit } = require('./batch18LabelAudit');
const { parseCsv, readZipEntries } = require('./strategyAudit');
const { ensureDir, parseArgs, readCsvRecords, readJson, writeJson } = require('./utils');

const manifest = require('../../services/strategy_route_manifest.json');
const baseScenario = require('../config/common');

async function main() {
  const args = parseArgs(process.argv);
  const sessionId = positiveInteger(args.sessionId, '--session-id');
  const runId = validateRunId(args.runId || `batch18-label-audit-${Date.now()}`);
  if (!args.sourceSummary) throw new Error('--source-summary is required');
  const sourceSummaryPath = path.resolve(args.sourceSummary);
  const sourceSummary = readJson(sourceSummaryPath);
  const scenario = {
    ...baseScenario,
    baseUrl: String(args.baseUrl || 'http://127.0.0.1:8000'),
    browser: { ...baseScenario.browser, headless: true },
    flow: { ...baseScenario.flow, loginAttempts: 3 }
  };
  const loginRows = readCsvRecords(path.resolve(args.students || scenario.studentsFile));
  const auditorRow = loginRows.find((row) => (
    String(row.role || '').trim().toLowerCase() === 'teacher' && row.login_key
  ));
  if (!auditorRow) throw new Error('Teacher login key is missing from the configured login CSV.');
  const groupRows = collectAuditGroups(loginRows);
  const uiGroupCode = String(args.uiGroupCode || 'G06').trim().toUpperCase();
  const browser = await chromium.launch({
    headless: true,
    args: scenario.browser.launchArgs
  });
  const context = await browser.newContext(scenario.context);
  const page = await context.newPage();
  let report;
  try {
    await login(page, {
      id: auditorRow.username || 'batch18-auditor',
      loginKey: auditorRow.login_key,
      role: 'teacher'
    }, scenario);
    const sessions = await fetchJson(page, '/api/teacher/sessions?all=true', scenario);
    const session = (sessions.sessions || []).find((item) => Number(item.id) === sessionId);
    if (!session) throw new Error(`Session ${sessionId} was not returned by the teacher sessions API.`);
    const groups = [];
    for (const group of groupRows) {
      groups.push(await captureGroup(page, scenario, sessionId, group));
      console.log(`[batch18] captured ${group.groupCode}`);
    }
    const ui = await captureTeacherUi(page, scenario, sessionId, uiGroupCode, groups);
    report = buildBatch18LabelAudit({
      sessionId,
      sourceSummary,
      session,
      groups,
      ui,
      manifest
    });
    report.run_id = runId;
    report.base_url = scenario.baseUrl;
    report.source_summary = sourceSummaryPath;
  } finally {
    await context.close().catch(() => null);
    await browser.close().catch(() => null);
  }

  const reportDir = path.resolve(args.reportDir || baseScenario.reportDir);
  ensureDir(reportDir);
  const jsonPath = path.join(reportDir, `${runId}-batch18-label-audit.json`);
  const markdownPath = path.join(reportDir, `${runId}-batch18-label-audit.md`);
  writeJson(jsonPath, report);
  fs.writeFileSync(markdownPath, formatMarkdown(report), 'utf8');
  console.log(JSON.stringify({
    passed: report.passed,
    counts: report.counts,
    stage2_health: report.stage2_health,
    strategy_compatibility: {
      selected_strategy_count: report.strategy_compatibility.selected_strategy_count,
      compatible_strategy_count: report.strategy_compatibility.compatible_strategy_count,
      compatibility_rate: report.strategy_compatibility.compatibility_rate
    },
    json_report: jsonPath,
    markdown_report: markdownPath
  }, null, 2));
  if (!report.passed) process.exitCode = 1;
}

async function captureGroup(page, scenario, sessionId, group) {
  const review = await fetchJson(
    page,
    `/api/teacher/group/${group.groupId}/emotion-review?session_id=${sessionId}&window_minutes=0`,
    scenario
  );
  const detail = await fetchJson(
    page,
    `/api/teacher/group/${group.groupId}/detail?session_id=${sessionId}`,
    scenario
  );
  const dbAudit = await fetchJson(
    page,
    `/api/teacher/group/${group.groupId}/state-suite-audit?session_id=${sessionId}`,
    scenario,
    120000
  );
  const messagesExport = await fetchSingleCsvExport(
    page, 'messages.csv', group.groupId, sessionId, 120000
  );
  const unifiedExport = await fetchSingleCsvExport(
    page, 'unified-events.csv', group.groupId, sessionId, 120000
  );
  return { ...group, review, detail, dbAudit, messagesExport, unifiedExport };
}

async function captureTeacherUi(page, scenario, sessionId, groupCode, groups) {
  const group = groups.find((item) => item.groupCode === groupCode);
  if (!group) throw new Error(`UI audit group ${groupCode} is not part of the six-group capture.`);
  await page.goto(`${scenario.baseUrl}/teacher/emotion-trend`, {
    waitUntil: 'domcontentloaded',
    timeout: 60000
  });
  await page.waitForFunction(() => (
    document.querySelector('#group-select')?.options.length > 1 &&
    document.querySelector('#session-select')?.options.length > 1
  ), null, { timeout: 60000 });
  await page.selectOption('#group-select', String(group.groupId));
  await page.selectOption('#session-select', String(sessionId));
  await page.$eval('#window-minutes', (element) => { element.value = '0'; });
  await page.evaluate(() => {
    if (window.etStopPolling) window.etStopPolling();
    return window.loadData();
  });
  const expectedRows = ((group.review || {}).messages || []).length;
  await page.waitForFunction((expected) => (
    document.querySelectorAll('#message-flow-area .emotion-message').length >= expected
  ), expectedRows, { timeout: 60000 });
  const messages = await page.$$eval('#message-flow-area .emotion-message', (nodes) => (
    nodes.map((node) => ({
      sequence: Number(node.dataset.sequence),
      display_state_label: String(
        node.querySelector('.state-cell .emotion-chip')?.textContent || ''
      ).trim()
    }))
  ));
  return { groupCode, groupId: group.groupId, messages };
}

async function fetchJson(page, url, scenario, timeoutMs = 60000) {
  const response = await browserFetchJson(page, url, {
    timeoutMs: Math.max(timeoutMs, Number(scenario.timeouts.apiResponseMs || 0))
  });
  return response.data || {};
}

async function fetchSingleCsvExport(page, filename, groupId, sessionId, timeoutMs) {
  const query = new URLSearchParams({
    group_id: String(groupId),
    session_id: String(sessionId),
    blind: '1'
  });
  const payload = await page.evaluate(async ({ url, timeoutMs }) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, {
        headers: { Accept: 'application/zip' },
        signal: controller.signal
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const bytes = new Uint8Array(await response.arrayBuffer());
      let binary = '';
      for (let index = 0; index < bytes.length; index += 0x8000) {
        binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
      }
      return { base64: btoa(binary) };
    } finally {
      clearTimeout(timer);
    }
  }, { url: `/export/${filename}?${query.toString()}`, timeoutMs });
  const entries = readZipEntries(Buffer.from(payload.base64, 'base64'));
  const entryName = Object.keys(entries).find((name) => (
    name === filename || name.endsWith(`/${filename}`)
  ));
  if (!entryName) throw new Error(`${filename} was not present in its export archive.`);
  return parseCsv(entries[entryName].toString('utf8'));
}

function collectAuditGroups(rows) {
  const groups = new Map();
  for (const row of rows) {
    if (String(row.role || '').toLowerCase() !== 'student') continue;
    const groupCode = String(row.group_code || '').trim().toUpperCase();
    if (!/^G0[1-6]$/.test(groupCode)) continue;
    const groupId = Number(row.group_id);
    if (groupId) groups.set(groupCode, { groupCode, groupId });
  }
  const result = [...groups.values()].sort((left, right) => left.groupCode.localeCompare(right.groupCode));
  if (result.length !== 6) throw new Error(`Expected G01-G06 in the login CSV, found ${result.length} groups.`);
  return result;
}

function formatMarkdown(report) {
  const counts = report.counts;
  const lines = [
    '# Batch 18 label validation',
    '',
    `- Result: ${report.passed ? 'PASS' : 'FAIL'}`,
    `- Session: ${report.session_id}`,
    `- Source run: ${report.source_run.run_id}`,
    `- Student display labels: ${counts.display_assigned_student_message_count}/${counts.student_message_count}`,
    `- Precise state: ${counts.precise_state_student_message_count}`,
    `- Unknown sub-state: ${counts.unknown_sub_state_student_message_count}`,
    `- Successful uncovered prefix: ${counts.success_uncovered_student_message_count}`,
    `- Failed assessment window: ${counts.failed_unclassified_student_message_count}`,
    `- Batch terminal: ${report.batch_health.batch_count - report.batch_health.nonterminal_batch_ids.length}/${report.batch_health.batch_count}`,
    `- Pipeline terminal: ${report.pipeline_health.pipeline_count - report.pipeline_health.nonterminal_pipeline_ids.length}/${report.pipeline_health.pipeline_count}`,
    `- Stage3 failed: ${report.pipeline_health.stage3_failed_pipeline_ids.length}`,
    `- Stage2 external attempts: ${report.stage2_health.external_attempt_count}`,
    `- Strategy route compatible: ${report.strategy_compatibility.compatible_strategy_count}/${report.strategy_compatibility.selected_strategy_count}`,
    `- Strict scenario/trigger coverage: diagnostic only (${report.strict_scenario_trigger_coverage.passed_case_count}/${report.strict_scenario_trigger_coverage.expected_case_count})`,
    '',
    '## Checks',
    '',
    ...Object.entries(report.checks).map(([name, passed]) => `- [${passed ? 'x' : ' '}] ${name}`),
    ''
  ];
  return lines.join('\n');
}

function positiveInteger(value, label) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) throw new Error(`${label} must be a positive integer`);
  return parsed;
}

function validateRunId(value) {
  const runId = String(value || '').trim();
  if (!/^[A-Za-z0-9._-]+$/.test(runId)) throw new Error('run id contains unsupported characters');
  return runId;
}

if (require.main === module) {
  main().catch((error) => {
    console.error(error.stack || error.message || error);
    process.exitCode = 1;
  });
}

module.exports = {
  captureGroup,
  collectAuditGroups,
  fetchSingleCsvExport,
  formatMarkdown
};
