const fs = require('fs');
const path = require('path');

function parseArgs(argv, env = process.env) {
  const args = {};
  const positional = [];
  const booleanArgs = new Set(['dryRun', 'headed', 'help', 'h']);
  for (let i = 2; i < argv.length; i += 1) {
    const token = argv[i];
    if (!token.startsWith('--')) {
      if (!args.baseUrl && /^https?:\/\//i.test(token)) {
        args.baseUrl = token;
      } else {
        positional.push(token);
      }
      continue;
    }
    const [rawKey, inlineValue] = token.slice(2).split('=', 2);
    const key = rawKey.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    if (inlineValue !== undefined) {
      args[key] = inlineValue;
      continue;
    }
    if (booleanArgs.has(key)) {
      args[key] = true;
      continue;
    }
    const next = argv[i + 1];
    if (!next || next.startsWith('--')) {
      args[key] = true;
      continue;
    }
    args[key] = next;
    i += 1;
  }
  applyNpmConfigArgs(args, env);
  applyPositionalFallbacks(args, positional);
  return args;
}

function applyNpmConfigArgs(args, env) {
  const mappings = {
    baseUrl: 'npm_config_base_url',
    students: 'npm_config_students',
    totalStudents: 'npm_config_total_students',
    groups: 'npm_config_groups',
    membersPerGroup: 'npm_config_members_per_group',
    durationMinutes: 'npm_config_duration_minutes',
    resourceMode: 'npm_config_resource_mode',
    headless: 'npm_config_headless',
    rampBatchSize: 'npm_config_ramp_batch_size',
    rampIntervalSeconds: 'npm_config_ramp_interval_seconds',
    stateCase: 'npm_config_state_case',
    fromStateCase: 'npm_config_from_state_case',
    suite: 'npm_config_suite',
    targetGroupCode: 'npm_config_target_group_code',
    expectedSessionId: 'npm_config_expected_session_id',
    runId: 'npm_config_run_id'
  };

  for (const [argKey, envKey] of Object.entries(mappings)) {
    if (args[argKey] !== undefined) continue;
    const value = getEnvValue(env, envKey);
    if (value === undefined || value === '' || value === 'true') continue;
    args[argKey] = value;
  }
}

function applyPositionalFallbacks(args, positional) {
  const numeric = positional.filter((value) => /^\d+$/.test(String(value)));
  const suites = new Set(['primary-substates', 'overlays', 'model-failure', 'agent-lock', 'p0-batch6', 'p0-batch6-direct']);
  const primaryStates = new Set([
    'standard',
    'deep_thinking',
    'execution_progress',
    'constructive_conflict',
    'interpersonal_conflict',
    'confusion',
    'frustration',
    'burnout',
    'off_topic_self_regulated',
    'off_topic_unregulated',
    'perfunctory_detachment',
    'individual_marginalization',
    'unknown_sub_state',
    'psychological_safety_risk',
    'high_intensity_overload',
    'stage_achievement'
  ]);
  for (const value of positional) {
    const normalized = String(value || '').trim().toLowerCase().replace(/-/g, '_');
    if (args.suite === undefined && suites.has(String(value || '').trim().toLowerCase())) {
      args.suite = String(value).trim().toLowerCase();
    } else if (
      args.stateCase === undefined &&
      (/^s(?:0[1-9]|1[0-6])$/i.test(String(value)) || primaryStates.has(normalized))
    ) {
      args.stateCase = value;
    }
  }
  if (args.rampBatchSize === undefined && numeric[0] !== undefined) {
    args.rampBatchSize = numeric[0];
  }
  if (args.rampIntervalSeconds === undefined && numeric[1] !== undefined) {
    args.rampIntervalSeconds = numeric[1];
  }
}

function getEnvValue(env, key) {
  if (env[key] !== undefined) return env[key];
  const found = Object.keys(env).find((candidate) => candidate.toLowerCase() === key.toLowerCase());
  return found ? env[found] : undefined;
}

function showHelp() {
  return [
    'Usage:',
    '  node src/run.js --scenario discussion-30m',
    '  node src/run.js --scenario discussion-60m',
    '  node src/run.js --scenario full-flow-30m',
    '  node src/run.js --scenario full-flow-60m',
    '  node src/run.js --scenario one-group-full-flow-trigger-states',
    '  node src/run.js --scenario trigger-states-4p',
    '  node src/run.js --scenario trigger-states-4p-60m',
    '  node src/run.js --scenario trigger-states-all-groups',
    '  node src/run.js --scenario trigger-states-all-groups-60m',
    '',
    'Options:',
    '  --scenario <name>          Scenario file in config/.',
    '  --students <path>          Student JSON/CSV file. Default: ../data/login_keys.csv.',
    '  --base-url <url>           Override target base URL.',
    '  --duration-minutes <n>     Override discussion duration.',
    '  --total-students <n>       Override number of students.',
    '  --groups <n>               Override expected group count.',
    '  --members-per-group <n>    Override expected members per group.',
    '  --state-case <id|state>     Run one isolated S01..S16 case by ID or state/overlay name.',
    '  --from-state-case <id>      Resume the full dialogue at S01..S16, preserving later boundaries.',
    '  --suite <name>              primary-substates, overlays, model-failure, agent-lock, p0-batch6, or p0-batch6-direct.',
    '  --target-group-code <code>  Run a one-group scenario against that login/audit group (for example G02).',
    '  --expected-session-id <n>   Refuse to audit a different running test session.',
    '  --run-id <id>               Stable report ID (letters, digits, dot, underscore, hyphen).',
    '  --ramp-batch-size <n>      Override ramp-up students per batch.',
    '  --ramp-interval-seconds <n> Override ramp-up interval.',
    '  --resource-mode <mode>     light or full.',
    '  --headed                  Run Chromium with UI.',
    '  --dry-run                 Validate config and print execution plan.'
  ].join('\n');
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function sleepUntil(ms, shouldStop) {
  const endAt = Date.now() + ms;
  while (Date.now() < endAt) {
    if (shouldStop && shouldStop()) return;
    await sleep(Math.min(1000, endAt - Date.now()));
  }
}

function randomInt(min, max) {
  return Math.floor(Math.random() * (max - min + 1)) + min;
}

function randomBetween(range) {
  return randomInt(range[0], range[1]);
}

function chance(probability) {
  return Math.random() < probability;
}

function pickRandom(items) {
  return items[randomInt(0, items.length - 1)];
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

function readCsvRecords(filePath) {
  const text = fs.readFileSync(filePath, 'utf8').replace(/^\uFEFF/, '');
  const rows = parseCsv(text);
  if (!rows.length) return [];
  const headers = rows[0].map((header) => header.trim());
  return rows.slice(1)
    .filter((row) => row.some((cell) => String(cell || '').trim() !== ''))
    .map((row) => {
      const record = {};
      headers.forEach((header, index) => {
        record[header] = row[index] === undefined ? '' : row[index];
      });
      return record;
    });
}

function parseCsv(text) {
  const rows = [];
  let row = [];
  let cell = '';
  let inQuotes = false;

  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    const next = text[i + 1];

    if (inQuotes) {
      if (ch === '"' && next === '"') {
        cell += '"';
        i += 1;
      } else if (ch === '"') {
        inQuotes = false;
      } else {
        cell += ch;
      }
      continue;
    }

    if (ch === '"') {
      inQuotes = true;
    } else if (ch === ',') {
      row.push(cell);
      cell = '';
    } else if (ch === '\n') {
      row.push(cell.replace(/\r$/, ''));
      rows.push(row);
      row = [];
      cell = '';
    } else {
      cell += ch;
    }
  }

  if (cell.length || row.length) {
    row.push(cell.replace(/\r$/, ''));
    rows.push(row);
  }

  return rows;
}

function writeJson(filePath, data) {
  fs.writeFileSync(filePath, `${JSON.stringify(data, null, 2)}\n`, 'utf8');
}

function absoluteUrl(baseUrl, urlOrPath) {
  return new URL(urlOrPath, baseUrl).toString();
}

function createRunId(name) {
  const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\..+$/, '').replace('T', '-');
  return `${name}-${stamp}`;
}

function percentile(values, p) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const idx = Math.ceil((p / 100) * sorted.length) - 1;
  return sorted[Math.max(0, Math.min(sorted.length - 1, idx))];
}

function avg(values) {
  if (!values.length) return null;
  return Math.round(values.reduce((sum, value) => sum + value, 0) / values.length);
}

function csvEscape(value) {
  if (value === undefined || value === null) return '';
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  if (/[",\n\r]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
  return text;
}

function loadScenario(name) {
  if (!name) throw new Error('Missing --scenario. Use --scenario discussion-30m or discussion-60m.');
  const filePath = path.resolve(__dirname, '..', 'config', `${name}.js`);
  if (!fs.existsSync(filePath)) {
    throw new Error(`Scenario file not found: ${filePath}`);
  }
  return require(filePath);
}

function applyCliOverrides(scenario, args) {
  const selectorCount = [args.stateCase, args.fromStateCase, args.suite].filter(Boolean).length;
  if (selectorCount > 1) {
    throw new Error('Choose only one of --state-case, --from-state-case, or --suite.');
  }
  let selectedScenario = scenario;
  if (args.stateCase) {
    if (typeof scenario.forStateCase !== 'function') {
      throw new Error(`Scenario ${scenario.name || ''} does not support --state-case.`);
    }
    selectedScenario = scenario.forStateCase(args.stateCase);
  } else if (args.fromStateCase) {
    if (typeof scenario.forStateRange !== 'function') {
      throw new Error(`Scenario ${scenario.name || ''} does not support --from-state-case.`);
    }
    selectedScenario = scenario.forStateRange(args.fromStateCase);
  } else if (args.suite) {
    if (typeof scenario.forSuite !== 'function') {
      throw new Error(`Scenario ${scenario.name || ''} does not support --suite.`);
    }
    selectedScenario = scenario.forSuite(args.suite);
  }

  const next = { ...selectedScenario };
  next.browser = { ...selectedScenario.browser };
  next.rampUp = { ...selectedScenario.rampUp };
  next.strategyAudit = selectedScenario.strategyAudit ? { ...selectedScenario.strategyAudit } : selectedScenario.strategyAudit;

  if (args.baseUrl) next.baseUrl = args.baseUrl;
  if (args.students) next.studentsFile = path.resolve(process.cwd(), args.students);
  if (args.totalStudents) next.totalStudents = Number(args.totalStudents);
  if (args.groups) {
    const requestedGroups = String(args.groups)
      .split(',')
      .map((value) => Number(String(value).trim()))
      .filter((value) => Number.isInteger(value) && value > 0);
    next.groupCount = requestedGroups.length > 1
      ? requestedGroups.length
      : Number(args.groups);
  }
  if (args.membersPerGroup) next.membersPerGroup = Number(args.membersPerGroup);
  if (args.durationMinutes) next.discussionDurationMs = Number(args.durationMinutes) * 60 * 1000;
  if (args.resourceMode) next.resourceMode = args.resourceMode;
  if (args.headed) next.browser.headless = false;
  if (args.headless) next.browser.headless = String(args.headless).toLowerCase() !== 'false';
  if (args.rampBatchSize) next.rampUp.batchSize = Number(args.rampBatchSize);
  if (args.rampIntervalSeconds) next.rampUp.intervalMs = Number(args.rampIntervalSeconds) * 1000;
  if (args.targetGroupCode) applyTargetGroupCode(next, args.targetGroupCode);
  if (args.expectedSessionId && next.strategyAudit) {
    next.strategyAudit.expectedSessionId = Number(args.expectedSessionId);
  }

  if (!next.minReadyStudents || next.minReadyStudents > next.totalStudents) {
    next.minReadyStudents = next.totalStudents;
  }
  return next;
}

function applyTargetGroupCode(scenario, rawGroupCode) {
  const groupCode = String(rawGroupCode || '').trim().toUpperCase();
  if (!/^G\d+$/.test(groupCode)) {
    throw new Error(`Invalid --target-group-code '${rawGroupCode}'. Expected a code such as G02.`);
  }
  if (Number(scenario.groupCount) !== 1) {
    throw new Error('--target-group-code is only supported for one-group scenarios.');
  }

  const retargetScenarios = (items) => (items || []).map((item) => ({
    ...item,
    groupCode
  }));
  const retargetMessage = (message) => ({
    ...message,
    groupCode,
    studentId: message.studentId
      ? String(message.studentId).replace(/-G\d+-M/i, `-${groupCode}-M`)
      : message.studentId
  });

  scenario.targetGroupCode = groupCode;
  scenario.name = `${scenario.name}-${groupCode.toLowerCase()}`;
  if (scenario.stateSuite) {
    scenario.stateSuite = {
      ...scenario.stateSuite,
      groupCode,
      scenarios: retargetScenarios(scenario.stateSuite.scenarios)
    };
  }
  if (scenario.scriptedDiscussion) {
    scenario.scriptedDiscussion = {
      ...scenario.scriptedDiscussion,
      scenarios: retargetScenarios(scenario.scriptedDiscussion.scenarios),
      expectedRuleStates: (scenario.scriptedDiscussion.expectedRuleStates || []).map((item) => ({
        ...item,
        groupCode
      })),
      messages: (scenario.scriptedDiscussion.messages || []).map(retargetMessage)
    };
  }
}

function formatDuration(ms) {
  const seconds = Math.round(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}m${String(rest).padStart(2, '0')}s`;
}

module.exports = {
  parseArgs,
  showHelp,
  sleep,
  sleepUntil,
  randomInt,
  randomBetween,
  chance,
  pickRandom,
  ensureDir,
  readJson,
  readCsvRecords,
  writeJson,
  absoluteUrl,
  createRunId,
  percentile,
  avg,
  csvEscape,
  loadScenario,
  applyCliOverrides,
  formatDuration
};
