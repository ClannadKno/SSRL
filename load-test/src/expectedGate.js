const TERMINAL_PIPELINE_STATUSES = Object.freeze(new Set([
  'PUBLISHED',
  'SUPPRESSED',
  'STALE',
  'SUPERSEDED',
  'FAILED',
  'SKIPPED',
  'NOT_PUBLISHED'
]));

function normalizeStatus(value) {
  return String(value || '').trim().toUpperCase().replace(/[-\s]+/g, '_');
}

function pipelineRunId(pipeline) {
  if (!pipeline || typeof pipeline !== 'object') return null;
  const value = pipeline.pipeline_run_id ?? pipeline.pipelineRunId ?? pipeline.id;
  return value === undefined || value === null || String(value) === '' ? null : String(value);
}

function pipelineStatuses(pipeline) {
  if (!pipeline || typeof pipeline !== 'object') return [];
  return [
    pipeline.final_status,
    pipeline.publish_status,
    pipeline.terminal_status,
    pipeline.status
  ].map(normalizeStatus).filter(Boolean);
}

function terminalForStatus(status) {
  const normalized = normalizeStatus(status);
  if (!normalized) return null;
  if (normalized === 'PUBLISHED' || normalized === 'ALREADY_PUBLISHED') return 'PUBLISHED';
  if (normalized === 'SUPPRESSED' || normalized.startsWith('SUPPRESSED_')) return 'SUPPRESSED';
  if (normalized === 'STALE') return 'STALE';
  if (normalized === 'SUPERSEDED') return 'SUPERSEDED';
  if (normalized === 'SKIPPED') return 'SKIPPED';
  if (
    normalized === 'FAILED' ||
    normalized === 'ERROR' ||
    normalized === 'FAILURE' ||
    normalized === 'TIMEOUT' ||
    normalized === 'CANCELLED'
  ) return 'FAILED';
  if (normalized === 'NOT_PUBLISHED') return 'NOT_PUBLISHED';
  return null;
}

function pipelineTerminal(pipeline) {
  const statuses = pipelineStatuses(pipeline);
  for (const status of statuses) {
    const reason = terminalForStatus(status);
    if (reason) {
      return {
        terminalReason: reason,
        terminalStatus: status,
        isTerminal: true
      };
    }
  }
  return {
    terminalReason: null,
    terminalStatus: null,
    isTerminal: false
  };
}

function pipelineCandidates(syncBody) {
  if (!syncBody || typeof syncBody !== 'object') return [];
  const candidates = [];
  const add = (value) => {
    if (!value) return;
    if (Array.isArray(value)) {
      value.forEach(add);
      return;
    }
    if (typeof value === 'object') candidates.push(value);
  };

  add(syncBody.pipelines);
  add(syncBody.pipeline_runs);
  add(syncBody.strategy_pipeline_runs);
  add(syncBody.pipeline);
  add(syncBody.expected_gate && syncBody.expected_gate.pipeline);

  const seen = new Set();
  return candidates.filter((item) => {
    const id = pipelineRunId(item);
    const key = id || `${item.trigger_message_id || item.triggerMessageId || ''}:${item.created_at || ''}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function selectPipeline(candidates, selectedId = null) {
  const items = Array.isArray(candidates) ? candidates : [];
  if (selectedId !== null && selectedId !== undefined) {
    const wanted = String(selectedId);
    const selected = items.find((item) => pipelineRunId(item) === wanted);
    if (selected) return selected;
  }
  return items[0] || null;
}

function replacementPipelineId(pipeline) {
  if (!pipeline || typeof pipeline !== 'object') return null;
  const value = pipeline.replacement_pipeline_run_id
    ?? pipeline.replaced_by_pipeline_run_id
    ?? pipeline.replacementPipelineRunId
    ?? pipeline.replacedByPipelineRunId;
  return value === undefined || value === null || String(value) === ''
    ? null
    : String(value);
}

function findReplacementPipeline(pipeline, candidates = []) {
  if (!pipeline || typeof pipeline !== 'object') return null;
  const inline = pipeline.replacement_pipeline || pipeline.replacementPipeline;
  if (inline && typeof inline === 'object') return inline;
  const replacementId = replacementPipelineId(pipeline);
  return replacementId === null
    ? null
    : selectPipeline(candidates, replacementId);
}

function pipelineReplacementReport(pipeline, candidates = []) {
  const originalId = pipelineRunId(pipeline);
  const originalTerminal = pipelineTerminal(pipeline);
  const visited = new Set(originalId ? [originalId] : []);
  let replacement = findReplacementPipeline(pipeline, candidates);
  while (replacement) {
    const replacementId = pipelineRunId(replacement);
    if (!replacementId || visited.has(replacementId)) break;
    visited.add(replacementId);
    const next = findReplacementPipeline(replacement, candidates);
    if (!next) break;
    replacement = next;
  }
  const replacementStage2Succeeded = replacement
    && normalizeStatus(replacement.stage2_status || replacement.stage2Status) === 'SUCCEEDED';
  const replacementState = replacement && (
    replacement.canonical_sub_state_code
    ?? replacement.canonicalSubStateCode
    ?? replacement.latest_state
    ?? replacement.latestState
    ?? null
  );
  const directReplacementId = replacementPipelineId(pipeline);
  return {
    original_pipeline_run_id: originalId,
    original_pipeline_terminal: pipeline && (
      pipeline.original_pipeline_terminal
      ?? pipeline.final_status
      ?? pipeline.finalStatus
      ?? (originalTerminal.isTerminal ? originalTerminal.terminalStatus : null)
    ),
    replacement_pipeline_run_id: replacement
      ? pipelineRunId(replacement)
      : directReplacementId,
    replacement_final_state: replacement
      ? (replacementStage2Succeeded ? replacementState : null)
      : ((pipeline && pipeline.replacement_final_state) ?? null),
    replacement_should_intervene: replacement
      ? (replacementStage2Succeeded
        ? (replacement.should_intervene
          ?? replacement.latest_should_intervene
          ?? replacement.latestShouldIntervene
          ?? null)
        : null)
      : ((pipeline && pipeline.replacement_should_intervene) ?? null),
    replacement_publish_status: replacement
      ? (replacement.publish_status
        ?? replacement.publishStatus
        ?? replacement.final_status
        ?? null)
      : ((pipeline && pipeline.replacement_publish_status) ?? null),
    trigger_level_state: pipeline && (
      pipeline.trigger_level_state ?? pipeline.triggerLevelState ?? null
    ),
    latest_state: pipeline && (
      pipeline.latest_state ?? pipeline.latestState ?? null
    ),
    latest_should_intervene: pipeline && (
      pipeline.latest_should_intervene ?? pipeline.latestShouldIntervene ?? null
    )
  };
}

function timestampValue(...values) {
  for (const value of values) {
    if (value === null || value === undefined || value === '') continue;
    const parsed = Date.parse(String(value));
    if (Number.isFinite(parsed)) return new Date(parsed).toISOString();
  }
  return null;
}

function durationMs(start, end) {
  const started = start ? Date.parse(String(start)) : NaN;
  const finished = end ? Date.parse(String(end)) : NaN;
  if (!Number.isFinite(started) || !Number.isFinite(finished)) return null;
  return Math.max(0, finished - started);
}

function pipelineTiming(pipeline, gateStartedAt = null, candidates = []) {
  const terminal = pipelineTerminal(pipeline);
  const replacement = pipelineReplacementReport(pipeline, candidates);
  const triggerMessageAt = timestampValue(
    pipeline && (pipeline.trigger_message_at || pipeline.triggerMessageAt),
    pipeline && pipeline.created_at,
    gateStartedAt
  );
  const pipelineTerminalAt = timestampValue(
    pipeline && (pipeline.pipeline_terminal_at || pipeline.pipelineTerminalAt),
    terminal.terminalReason === 'PUBLISHED' && pipeline && pipeline.published_at,
    pipeline && pipeline.terminal_at,
    terminal.isTerminal && pipeline && pipeline.updated_at
  );
  const leaseAcquiredAt = timestampValue(
    pipeline && (pipeline.lease_acquired_at || pipeline.leaseAcquiredAt),
    pipeline && pipeline.room_lock_acquired_at
  );
  const leaseReleasedAt = timestampValue(
    pipeline && (pipeline.lease_released_at || pipeline.leaseReleasedAt),
    pipeline && pipeline.room_lock_released_at
  );
  return {
    ...replacement,
    pipeline_run_id: pipelineRunId(pipeline),
    trigger_message_id: pipeline && (
      pipeline.trigger_message_id ?? pipeline.triggerMessageId ?? null
    ),
    trigger_message_at: triggerMessageAt,
    pipeline_terminal_at: pipelineTerminalAt,
    pipeline_wait_duration: durationMs(triggerMessageAt, pipelineTerminalAt),
    lease_acquired_at: leaseAcquiredAt,
    lease_released_at: leaseReleasedAt,
    lease_duration: durationMs(leaseAcquiredAt, leaseReleasedAt),
    terminal_reason: terminal.terminalReason,
    terminal_status: terminal.terminalStatus
  };
}

module.exports = {
  TERMINAL_PIPELINE_STATUSES,
  durationMs,
  normalizeStatus,
  pipelineCandidates,
  findReplacementPipeline,
  pipelineRunId,
  pipelineReplacementReport,
  pipelineStatuses,
  pipelineTerminal,
  pipelineTiming,
  selectPipeline,
  timestampValue
};
