const path = require('path');
const zlib = require('zlib');
const { createStudentContext } = require('./browser');
const { browserFetchJson, login } = require('./actions');

const TERMINAL_PIPELINE_STATUSES = new Set([
  'PUBLISHED',
  'SUPPRESSED',
  'COMPLETED',
  'SKIPPED',
  'FAILED',
  'PASS',
  'CANCELLED',
  'STALE',
  'EXPIRED'
]);

const REQUIRED_EXPORTS = [
  'messages.csv',
  'strategy_pipeline_runs.csv',
  'interventions.csv',
  'unified-events.csv'
];

const EXPORT_SAFE_FIELDS = {
  'messages.csv': [
    'message_id', 'id', 'sequence', 'sender_type', 'role',
    'group_id', 'group_code', 'session_id', 'discussion_id', 'task_id',
    'agent_type', 'trigger_source', 'final_sub_state_code',
    'assessment_status', 'assignment_source', 'inferred',
    'coarse_state_code', 'legacy_state_code', 'state_overlays',
    'segment_id', 'assessment_batch_id', 'strategy_pipeline_run_id',
    'should_intervene', 'selected_strategy_id', 'inhibition_strategy_id',
    'error_code', 'failure_detail'
  ],
  'strategy_pipeline_runs.csv': [
    'pipeline_run_id', 'run_uuid', 'group_id', 'group_code',
    'session_id', 'discussion_id', 'task_id',
    'trigger_source', 'trigger_message_id',
    'input_start_sequence', 'input_end_sequence',
    'canonical_sub_state_code', 'coarse_state_code', 'state_overlays',
    'sub_state_start_sequence', 'sub_state_end_sequence',
    'evidence_message_ids', 'should_intervene',
    'inhibition_strategy_id', 'candidate_strategy_ids',
    'selected_strategy_id', 'supporting_strategy_ids',
    'strategy_library_version', 'publish_status', 'published_message_id',
    'final_status', 'failure_code', 'failure_detail', 'skip_reason'
  ],
  'interventions.csv': [
    'intervention_run_id', 'id', 'group_id', 'group_code',
    'session_id', 'discussion_id', 'task_id',
    'strategy_pipeline_run_id', 'canonical_sub_state_code',
    'coarse_state_code', 'assessment_status', 'assignment_source',
    'selected_strategy_id', 'inhibition_strategy_id', 'strategy_id',
    'should_intervene', 'status', 'publish_status', 'message_id',
    'error_code', 'failure_detail', 'failure_reason', 'skip_reason',
    'agent_type', 'trigger_source'
  ],
  'unified-events.csv': [
    'event_type', 'event_id', 'event_subtype', 'event_key',
    'source_export_name', 'source_table', 'source',
    'group_id', 'group_code', 'session_id', 'discussion_id', 'task_id',
    'actor_role', 'related_table', 'related_id',
    'final_sub_state_code', 'coarse_state_code', 'legacy_state_code',
    'assessment_status', 'assignment_source', 'inferred',
    'segment_id', 'assessment_batch_id', 'strategy_pipeline_run_id',
    'selected_strategy_id', 'inhibition_strategy_id',
    'error_code', 'failure_detail'
  ]
};

function pick(item, fields) {
  return Object.fromEntries(fields.map((field) => [field, item && item[field]]));
}

function sanitizeAgentAudit(audit) {
  const source = audit || {};
  return {
    message_timeline: (source.message_timeline || []).map((item) => pick(item, [
      'id', 'sequence', 'role', 'sender_type', 'group_id', 'session_id',
      'discussion_id', 'task_id', 'agent_type', 'trigger_source',
      'final_sub_state_code', 'assessment_status', 'assignment_source',
      'inferred', 'coarse_state_code', 'strategy_id'
    ])),
    assessment_batches: (source.assessment_batches || []).map((item) => pick(item, [
      'id', 'batch_id', 'group_id', 'session_id', 'discussion_id', 'task_id',
      'candidate_start_sequence', 'candidate_end_sequence',
      'status', 'terminal_status', 'error_code', 'fallback_action',
      'fallback_segment_count', 'assignment_source'
    ])),
    strategy_pipeline_runs: (source.strategy_pipeline_runs || []).map((item) => pick(item, [
      'id', 'pipeline_run_id', 'run_uuid', 'group_id', 'group_code',
      'session_id', 'discussion_id', 'task_id',
      'trigger_source', 'trigger_message_id',
      'input_start_sequence', 'input_end_sequence',
      'canonical_sub_state_code', 'final_sub_state_code',
      'coarse_state_code', 'secondary_sub_state_tags',
      'sub_state_start_sequence', 'sub_state_end_sequence',
      'evidence_message_ids', 'evidence_sequences',
      'detected_self_regulation', 'should_intervene', 'inhibition_strategy_id',
      'strategy_candidate_ids', 'selected_strategy_id',
      'supporting_strategy_ids', 'publish_status', 'published_message_id',
      'final_status', 'failure_code', 'failure_detail', 'skip_reason'
    ])),
    interventions: (source.interventions || []).map((item) => pick(item, [
      'id', 'intervention_run_id', 'group_id', 'session_id',
      'discussion_id', 'task_id', 'strategy_pipeline_run_id',
      'canonical_sub_state_code', 'selected_strategy_id', 'strategy_id',
      'agent_type', 'status', 'publish_status', 'message_id',
      'failure_reason', 'skip_reason'
    ])),
    traceability_warnings: source.traceability_warnings || []
  };
}

function sanitizeTrend(trend) {
  const source = trend || {};
  return {
    group_id: source.group_id,
    session_id: source.session_id,
    requested_session_id: source.requested_session_id,
    resolved_session_id: source.resolved_session_id,
    message_state_context: pick(source.message_state_context || {}, [
      'discussion_id', 'processing_mode', 'message_assignment_policy',
      'message_assignment_summary'
    ]),
    message_assignment_summary: source.message_assignment_summary || {},
    current_state: pick(source.current_state || {}, [
      'final_sub_state_code', 'assessment_status', 'assignment_source',
      'inferred', 'coarse_state_code', 'segment_id'
    ]),
    state_segments: (source.state_segments || []).map((item) => pick(item, [
      'id', 'segment_id', 'discussion_id', 'start_sequence', 'end_sequence',
      'final_sub_state_code', 'canonical_sub_state_code',
      'assessment_status', 'assignment_source', 'coarse_state_code',
      'state_overlays', 'selected_strategy_id',
      'inhibition_strategy_id', 'strategy_pipeline_run_id'
    ])),
    summary: source.summary || {},
    distribution: source.distribution || {}
  };
}

function sanitizeReview(review) {
  const source = review || {};
  return {
    group_id: source.group_id,
    session_id: source.session_id,
    requested_session_id: source.requested_session_id,
    resolved_session_id: source.resolved_session_id,
    message_state_context: pick(source.message_state_context || {}, [
      'discussion_id', 'processing_mode', 'message_assignment_summary'
    ]),
    message_assignment_summary: source.message_assignment_summary || {},
    current_state: pick(source.current_state || {}, [
      'final_sub_state_code', 'assessment_status', 'assignment_source',
      'coarse_state_code'
    ]),
    messages: (source.messages || []).map((item) => pick(item, [
      'id', 'sequence', 'role', 'sender_type', 'session_id', 'discussion_id',
      'task_id', 'agent_type', 'trigger_source',
      'final_sub_state_code', 'assessment_status', 'assignment_source',
      'inferred', 'coarse_state_code', 'selected_strategy_id',
      'inhibition_strategy_id', 'strategy_pipeline_run_id', 'error_code'
    ])),
    state_segments: (source.state_segments || []).map((item) => pick(item, [
      'id', 'segment_id', 'discussion_id', 'start_sequence', 'end_sequence',
      'final_sub_state_code', 'canonical_sub_state_code',
      'assessment_status', 'assignment_source', 'coarse_state_code'
    ])),
    summary: source.summary || {}
  };
}

function sanitizeTeacherGroups(payload) {
  return {
    groups: ((payload && payload.groups) || []).map((item) => pick(item, [
      'group_id', 'group_code', 'session_id',
      'final_sub_state_code', 'state_code', 'assessment_status',
      'assignment_source', 'inferred', 'coarse_state_code'
    ]))
  };
}

function sanitizeGroupDetail(payload) {
  const source = payload || {};
  return {
    session_id: source.session_id,
    current_state: pick(source.current_state || {}, [
      'final_sub_state_code', 'assessment_status', 'assignment_source',
      'inferred', 'coarse_state_code'
    ]),
    canonical_segments: (source.canonical_segments || []).map((item) => pick(item, [
      'id', 'segment_id', 'discussion_id', 'start_sequence', 'end_sequence',
      'final_sub_state_code', 'canonical_sub_state_code',
      'assessment_status', 'assignment_source', 'coarse_state_code'
    ])),
    message_assignment_summary: source.message_assignment_summary || {}
  };
}

async function createStrategyAuditSession({
  browser,
  scenario,
  auditor,
  contextFactory = createStudentContext,
  loginFn = login
}) {
  if (!auditor || !auditor.loginKey) {
    throw new Error('Strategy audit requires a teacher login key in the configured login CSV.');
  }

  const context = await contextFactory(browser, scenario, auditor);
  try {
    const page = await context.newPage();
    await loginFn(page, auditor, scenario);
    return { context, page, closed: false };
  } catch (error) {
    await context.close().catch(() => null);
    throw error;
  }
}

async function closeStrategyAuditSession(session) {
  if (!session || session.closed) return;
  session.closed = true;
  await session.context.close().catch(() => null);
}

async function captureStrategyAuditSnapshot({
  browser,
  scenario,
  auditor,
  students,
  phase,
  auditSession = null
}) {
  const ownedSession = auditSession
    ? null
    : await createStrategyAuditSession({ browser, scenario, auditor });
  const session = auditSession || ownedSession;
  if (!session || session.closed || !session.page) {
    throw new Error('Strategy audit session is unavailable or already closed.');
  }

  try {
    const page = session.page;
    const statusResponse = await browserFetchJson(page, '/api/teacher/status/current', {
      timeoutMs: scenario.timeouts.apiResponseMs
    });
    const currentSession = statusResponse.data && statusResponse.data.current_session;
    const sessionId = Number(currentSession && (currentSession.id || currentSession.session_id));
    if (!sessionId) {
      throw new Error(
        'No current running discussion session exists on the target server. ' +
        'Start a fresh session with strategy detection enabled, then rerun the test.'
      );
    }
    const expectedSessionId = Number(scenario.strategyAudit && scenario.strategyAudit.expectedSessionId);
    if (expectedSessionId && sessionId !== expectedSessionId) {
      throw new Error(
        `Strategy audit expected session ${expectedSessionId}, but the server reports session ${sessionId}.`
      );
    }
    const agentFlags = (statusResponse.data && statusResponse.data.agent_flags) || {};
    const detectionEnabled = Boolean(
      agentFlags.detection_enabled === undefined
        ? statusResponse.data.detection_enabled
        : agentFlags.detection_enabled
    );
    const strategyEnabled = Boolean(
      agentFlags.strategy_agent_enabled === undefined
        ? statusResponse.data.strategy_agent_enabled
        : agentFlags.strategy_agent_enabled
    );
    const emotionEnabled = Boolean(
      agentFlags.emotion_agent_enabled === undefined
        ? statusResponse.data.emotion_agent_enabled
        : agentFlags.emotion_agent_enabled
    );
    if (scenario.strategyAudit.requireActualCoverage && (!detectionEnabled || !strategyEnabled)) {
      throw new Error(
        `Target session cannot produce strategy coverage: detection_enabled=${detectionEnabled}, ` +
        `strategy_agent_enabled=${strategyEnabled}.`
      );
    }
    assertAgentFlagCompatibility({
      scenario,
      currentSession,
      sessionId,
      emotionEnabled
    });

    const teacherGroupsResponse = await browserFetchJson(
      page,
      `/api/teacher/groups?all=1&session_id=${sessionId}`,
      { timeoutMs: scenario.timeouts.apiResponseMs }
    );
    const teacherGroups = sanitizeTeacherGroups(teacherGroupsResponse.data);
    const groupIds = groupIdsByCode(students);
    const stateScenarios = scenario.scriptedDiscussion.scenarios || [];
    const groups = [];
    const uniqueGroupScenarios = [...new Map(
      stateScenarios.map((item) => [item.groupCode, item])
    ).values()];
    for (const stateScenario of uniqueGroupScenarios) {
      const groupId = groupIds.get(stateScenario.groupCode);
      if (!groupId) throw new Error(`Missing group_id for ${stateScenario.groupCode}.`);
      const auditUrl =
        `/api/teacher/group/${groupId}/agent-audit?session_id=${sessionId}&blinded=true`;
      const dbAuditUrl =
        `/api/teacher/group/${groupId}/state-suite-audit?session_id=${sessionId}`;
      const allowMissingBaselineDiscussion = phase === 'baseline' && Boolean(
        scenario.strategyAudit && scenario.strategyAudit.requireCleanContext
      );
      const [auditResponse, dbAuditResponse, trendResponse, reviewResponse, detailResponse] =
        await Promise.all([
          browserFetchJson(page, auditUrl, {
            timeoutMs: Math.max(scenario.timeouts.apiResponseMs, 60 * 1000)
          }),
          browserFetchJson(page, dbAuditUrl, {
            timeoutMs: Math.max(scenario.timeouts.apiResponseMs, 60 * 1000),
            okStatuses: allowMissingBaselineDiscussion ? [200, 400] : [200]
          }),
          browserFetchJson(
            page,
            `/api/teacher/group/${groupId}/emotion-trend?session_id=${sessionId}&window_minutes=0`,
            { timeoutMs: Math.max(scenario.timeouts.apiResponseMs, 60 * 1000) }
          ),
          browserFetchJson(
            page,
            `/api/teacher/group/${groupId}/emotion-review?session_id=${sessionId}&window_minutes=0`,
            { timeoutMs: Math.max(scenario.timeouts.apiResponseMs, 60 * 1000) }
          ),
          browserFetchJson(
            page,
            `/api/teacher/group/${groupId}/detail?session_id=${sessionId}`,
            { timeoutMs: Math.max(scenario.timeouts.apiResponseMs, 60 * 1000) }
          )
        ]);
      let dbAudit = dbAuditResponse.data;
      if (dbAuditResponse.status === 400) {
        const error = String((dbAuditResponse.data && dbAuditResponse.data.error) || '');
        if (!allowMissingBaselineDiscussion || error !== 'no discussion exists for the requested group/session') {
          throw new Error(`HTTP 400: ${JSON.stringify(dbAuditResponse.data || {})}`);
        }
        dbAudit = buildEmptyStateSuiteBaseline({
          groupId,
          sessionId,
          taskId: currentSession.task_id || null
        });
      }
      const exportAudit = await fetchExportAudit(page, {
        groupId,
        sessionId,
        timeoutMs: Math.max(scenario.timeouts.apiResponseMs, 120 * 1000)
      });
      groups.push({
        scenarioId: stateScenario.id,
        groupCode: stateScenario.groupCode,
        groupId,
        audit: sanitizeAgentAudit(auditResponse.data),
        dbAudit,
        teacherApis: {
          emotionTrend: sanitizeTrend(trendResponse.data),
          emotionReview: sanitizeReview(reviewResponse.data),
          agentAudit: sanitizeAgentAudit(auditResponse.data),
          teacherGroups,
          groupDetail: sanitizeGroupDetail(detailResponse.data)
        },
        exportAudit
      });
    }

    return {
      phase,
      validationMode: 'real_coverage',
      capturedAt: new Date().toISOString(),
      sessionId,
      sessionNo: currentSession.session_no || null,
      auditorId: auditor.id,
      auditAvailable: groups.length > 0 && groups.every((group) => (
        group.dbAudit && group.dbAudit.audit_available &&
        group.teacherApis && group.exportAudit && group.exportAudit.available
      )),
      agentFlags: {
        detectionEnabled,
        strategyAgentEnabled: strategyEnabled,
        emotionAgentEnabled: emotionEnabled
      },
      groups
    };
  } finally {
    if (ownedSession) await closeStrategyAuditSession(ownedSession);
  }
}

async function browserFetchBinary(page, url, { timeoutMs = 120000 } = {}) {
  return page.evaluate(async ({ url, timeoutMs }) => {
    const token = new URLSearchParams(window.location.search).get('tab_token') || '';
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    const headers = { Accept: 'application/zip' };
    if (token) headers['X-Tab-Token'] = token;
    try {
      const response = await fetch(url, { headers, signal: controller.signal });
      if (!response.ok) {
        const text = await response.text();
        throw new Error(`HTTP ${response.status}: ${text.slice(0, 500)}`);
      }
      const bytes = new Uint8Array(await response.arrayBuffer());
      let binary = '';
      const chunkSize = 0x8000;
      for (let index = 0; index < bytes.length; index += chunkSize) {
        binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize));
      }
      return {
        base64: btoa(binary),
        contentType: response.headers.get('content-type') || '',
        contentLength: bytes.length
      };
    } finally {
      clearTimeout(timeout);
    }
  }, { url, timeoutMs });
}

async function fetchExportAudit(page, { groupId, sessionId, timeoutMs }) {
  const query = new URLSearchParams({
    group_id: String(groupId),
    session_id: String(sessionId),
    blind: '1'
  });
  const response = await browserFetchBinary(
    page,
    `/export/all?${query.toString()}`,
    { timeoutMs }
  );
  const entries = readZipEntries(Buffer.from(response.base64, 'base64'));
  return buildExportAuditFromEntries(entries, {
    contentLength: response.contentLength,
    contentType: response.contentType
  });
}

function readZipEntries(buffer) {
  const eocd = findSignatureBackwards(buffer, 0x06054b50);
  if (eocd < 0) throw new Error('Export response is not a valid ZIP archive.');
  const entryCount = buffer.readUInt16LE(eocd + 10);
  let offset = buffer.readUInt32LE(eocd + 16);
  const entries = {};
  for (let index = 0; index < entryCount; index += 1) {
    if (buffer.readUInt32LE(offset) !== 0x02014b50) {
      throw new Error(`Invalid ZIP central directory entry at offset ${offset}.`);
    }
    const flags = buffer.readUInt16LE(offset + 8);
    const method = buffer.readUInt16LE(offset + 10);
    const compressedSize = buffer.readUInt32LE(offset + 20);
    const fileNameLength = buffer.readUInt16LE(offset + 28);
    const extraLength = buffer.readUInt16LE(offset + 30);
    const commentLength = buffer.readUInt16LE(offset + 32);
    const localOffset = buffer.readUInt32LE(offset + 42);
    const name = buffer
      .subarray(offset + 46, offset + 46 + fileNameLength)
      .toString((flags & 0x800) ? 'utf8' : 'utf8');
    if (buffer.readUInt32LE(localOffset) !== 0x04034b50) {
      throw new Error(`Invalid ZIP local header for ${name}.`);
    }
    if (flags & 0x1) throw new Error(`Encrypted ZIP entry is unsupported: ${name}.`);
    const localNameLength = buffer.readUInt16LE(localOffset + 26);
    const localExtraLength = buffer.readUInt16LE(localOffset + 28);
    const dataStart = localOffset + 30 + localNameLength + localExtraLength;
    const compressed = buffer.subarray(dataStart, dataStart + compressedSize);
    let data;
    if (method === 0) data = compressed;
    else if (method === 8) data = zlib.inflateRawSync(compressed);
    else throw new Error(`Unsupported ZIP compression method ${method} for ${name}.`);
    entries[name] = data;
    offset += 46 + fileNameLength + extraLength + commentLength;
  }
  return entries;
}

function findSignatureBackwards(buffer, signature) {
  const minimum = Math.max(0, buffer.length - 0xffff - 22);
  for (let offset = buffer.length - 22; offset >= minimum; offset -= 1) {
    if (buffer.readUInt32LE(offset) === signature) return offset;
  }
  return -1;
}

function parseCsv(text) {
  const input = String(text || '').replace(/^\uFEFF/, '');
  const rows = [];
  let row = [];
  let field = '';
  let quoted = false;
  for (let index = 0; index < input.length; index += 1) {
    const char = input[index];
    if (quoted) {
      if (char === '"' && input[index + 1] === '"') {
        field += '"';
        index += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        field += char;
      }
    } else if (char === '"') {
      quoted = true;
    } else if (char === ',') {
      row.push(field);
      field = '';
    } else if (char === '\n') {
      row.push(field.replace(/\r$/, ''));
      rows.push(row);
      row = [];
      field = '';
    } else {
      field += char;
    }
  }
  if (field || row.length) {
    row.push(field.replace(/\r$/, ''));
    rows.push(row);
  }
  if (!rows.length) return [];
  const headers = rows.shift().map((item) => item.trim());
  return rows
    .filter((items) => items.some((item) => item !== ''))
    .map((items) => Object.fromEntries(
      headers.map((header, index) => [header, items[index] === undefined ? '' : items[index]])
    ));
}

function buildExportAuditFromEntries(entries, metadata = {}) {
  const files = {};
  const entryNames = Object.keys(entries || {});
  for (const filename of REQUIRED_EXPORTS) {
    const matching = entryNames.filter((name) => path.posix.basename(name) === filename);
    files[filename] = matching.flatMap((name) => {
      const value = entries[name];
      const text = Buffer.isBuffer(value) ? value.toString('utf8') : String(value || '');
      return parseCsv(text).map((row) => pick(row, EXPORT_SAFE_FIELDS[filename]));
    });
  }
  const manifestName = entryNames.find((name) => path.posix.basename(name) === 'manifest.json');
  let manifest = null;
  if (manifestName) {
    try {
      const value = entries[manifestName];
      manifest = JSON.parse(Buffer.isBuffer(value) ? value.toString('utf8') : String(value));
    } catch (_error) {
      manifest = null;
    }
  }
  const missingFiles = REQUIRED_EXPORTS.filter(
    (filename) => !entryNames.some((name) => path.posix.basename(name) === filename)
  );
  return {
    available: missingFiles.length === 0,
    missingFiles,
    schemaVersion: manifest && manifest.schema_version,
    files,
    contentLength: metadata.contentLength || null,
    contentType: metadata.contentType || null
  };
}

function assertCleanStrategyAuditBaseline(scenario, snapshot) {
  if (!(scenario.strategyAudit && scenario.strategyAudit.requireCleanContext)) return;
  const dirty = [];
  for (const group of (snapshot && snapshot.groups) || []) {
    const tables = (group.dbAudit && group.dbAudit.tables) || {};
    const studentMessages = (tables.messages || [])
      .filter((message) => String(message.role || message.sender_type || '').toLowerCase() === 'student');
    const activeRuns = (tables.strategy_pipeline_runs || [])
      .filter((run) => !TERMINAL_PIPELINE_STATUSES.has(String(run.final_status || '').toUpperCase()));
    if (studentMessages.length || activeRuns.length) {
      dirty.push(
        `${group.groupCode}(studentMessages=${studentMessages.length}, activePipelineRuns=${activeRuns.length})`
      );
    }
  }
  if (dirty.length) {
    throw new Error(
      'State scenarios require clean, independent discussions. Start a fresh session before running; dirty groups: ' +
      dirty.join(', ')
    );
  }
}

function buildEmptyStateSuiteBaseline({ groupId, sessionId, taskId = null }) {
  const tableNames = [
    'messages',
    'collaboration_state_segments',
    'state_assessment_batches',
    'strategy_pipeline_runs',
    'intervention_runs',
    'emotion_reflection_slots',
    'strategy_pipeline_latency_events'
  ];
  return {
    schema_version: 'state-suite-audit/2',
    audit_available: true,
    empty_baseline_without_discussion: true,
    available_tables: Object.fromEntries(tableNames.map((name) => [name, true])),
    scope: {
      group_id: Number(groupId),
      session_id: Number(sessionId),
      discussion_id: null,
      task_id: taskId === null ? null : Number(taskId)
    },
    tables: Object.fromEntries(tableNames.map((name) => [name, []])),
    room_lock: {
      locked: false,
      owner: null,
      lock_token_hash: null,
      complete_lock_token_included: false,
      expires_at: null
    }
  };
}

function groupIdsByCode(students) {
  const result = new Map();
  for (const student of students || []) {
    const groupCode = student.group_code || student.groupCode;
    const groupId = Number(student.group_id || student.groupId);
    if (groupCode && groupId) result.set(groupCode, groupId);
  }
  return result;
}

function assertAgentFlagCompatibility({
  scenario,
  currentSession = {},
  sessionId,
  emotionEnabled
}) {
  const expectedEmotion = scenario &&
    scenario.strategyAudit &&
    scenario.strategyAudit.expectedEmotionAgentEnabled;
  if (expectedEmotion === undefined) return;
  if (emotionEnabled === Boolean(expectedEmotion)) return;
  throw new Error(formatEmotionAgentMismatch({
    scenario,
    currentSession,
    sessionId,
    emotionEnabled,
    expectedEmotion
  }));
}

function formatEmotionAgentMismatch({
  scenario,
  currentSession = {},
  sessionId,
  emotionEnabled,
  expectedEmotion
}) {
  const expected = Boolean(expectedEmotion);
  const suite = (
    (scenario && scenario.stateSuite && scenario.stateSuite.mode) ||
    (scenario && scenario.name) ||
    'state-suite'
  );
  const sessionLabel = [
    `session ${sessionId || currentSession.id || currentSession.session_id || 'unknown'}`,
    currentSession.session_no === undefined ? null : `no.${currentSession.session_no}`,
    currentSession.status ? `status=${currentSession.status}` : null
  ].filter(Boolean).join(', ');
  const reason = expected
    ? 'The agent-lock suite exercises dual-Agent lock recovery, so the emotion Agent must be enabled.'
    : 'Primary, overlay, and model-failure state suites require the emotion Agent disabled so strategy coverage is not contaminated by emotion-Agent traffic.';
  const remedy = expected
    ? 'Start a fresh test session with the emotion Agent enabled, or choose --suite primary-substates/overlays/model-failure for emotion-Agent-off coverage.'
    : 'Start a fresh test session with the emotion Agent disabled, or run --suite agent-lock if you intended to test dual-Agent lock recovery.';
  return [
    `State suite '${suite}' requires emotion_agent_enabled=${expected}, but ${sessionLabel} reports ${Boolean(emotionEnabled)}.`,
    reason,
    remedy,
    'Use --expected-session-id <id> to prevent attaching to the wrong running session.'
  ].join(' ');
}

module.exports = {
  REQUIRED_EXPORTS,
  assertAgentFlagCompatibility,
  assertCleanStrategyAuditBaseline,
  buildEmptyStateSuiteBaseline,
  buildExportAuditFromEntries,
  captureStrategyAuditSnapshot,
  closeStrategyAuditSession,
  createStrategyAuditSession,
  parseCsv,
  readZipEntries,
  sanitizeAgentAudit
};
