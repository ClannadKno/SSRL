const fs = require('fs');
const path = require('path');

const TERMINAL_PIPELINE_STATUSES = new Set([
  'PUBLISHED', 'PASS', 'OI', 'SUPPRESSED', 'FAILED', 'STALE', 'SKIPPED',
  'NO_INTERVENTION', 'TEXT_VALIDATION_FAILED', 'WORKER_EXCEPTION', 'EXPIRED'
]);

function rowsForSnapshot(snapshot, tableName) {
  return (snapshot && Array.isArray(snapshot.groups) ? snapshot.groups : [])
    .flatMap((group) => (((group.dbAudit || {}).tables || {})[tableName] || []));
}

function newRows(baseline, finalSnapshot, tableName) {
  const seen = new Set(rowsForSnapshot(baseline, tableName).map(rowIdentity));
  return rowsForSnapshot(finalSnapshot, tableName)
    .filter((row) => !seen.has(rowIdentity(row)));
}

function rowIdentity(row) {
  if (row && row.id !== undefined && row.id !== null) return `id:${row.id}`;
  return JSON.stringify(row || {});
}

function upper(value) {
  return String(value || '').trim().toUpperCase();
}

function containsLeaseExpiry(run) {
  return /(?:ROOM_)?LEASE_EXPIRED/.test(
    `${upper(run.failure_code)} ${upper(run.skip_reason)} ${upper(run.final_status)}`
  );
}

function isPublished(run) {
  return upper(run.publish_status) === 'PUBLISHED' || Boolean(run.published_message_id);
}

function isTerminal(run) {
  const finalStatus = upper(run.final_status);
  return Boolean(finalStatus) && (
    TERMINAL_PIPELINE_STATUSES.has(finalStatus) ||
    !['PENDING', 'RUNNING', 'CLAIMED', 'READY_TO_PUBLISH'].includes(finalStatus)
  );
}

function isFaultInjection(run) {
  return /FAULT|INJECT|CHAOS/.test(upper(run.trigger_source));
}

function duplicateEmotionMessageCount(messages) {
  const counts = new Map();
  for (const message of messages) {
    if (upper(message.agent_type) !== 'EMOTION') continue;
    const key = message.intervention_run_id == null
      ? `message:${message.id}`
      : `run:${message.intervention_run_id}`;
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return [...counts.values()].reduce((total, count) => total + Math.max(0, count - 1), 0);
}

function parseTime(value) {
  const parsed = Date.parse(value || '');
  return Number.isFinite(parsed) ? parsed : null;
}

function lockUnion(pipelines, messages) {
  const intervals = pipelines
    .map((run) => [parseTime(run.room_lock_acquired_at), parseTime(run.room_lock_released_at)])
    .filter(([start, end]) => start !== null && end !== null && end >= start)
    .sort((a, b) => a[0] - b[0]);
  const merged = [];
  for (const interval of intervals) {
    const current = merged.at(-1);
    if (!current || interval[0] > current[1]) merged.push([...interval]);
    else current[1] = Math.max(current[1], interval[1]);
  }
  const unionMs = merged.reduce((total, [start, end]) => total + end - start, 0);
  const messageTimes = messages.map((message) => parseTime(message.created_at)).filter((item) => item !== null);
  const windowStart = messageTimes.length ? Math.min(...messageTimes) : null;
  const windowEndCandidates = [
    ...messageTimes,
    ...intervals.flat()
  ].filter((item) => item !== null);
  const windowEnd = windowEndCandidates.length ? Math.max(...windowEndCandidates) : null;
  const windowMs = windowStart !== null && windowEnd !== null && windowEnd >= windowStart
    ? windowEnd - windowStart
    : null;
  return {
    interval_count: intervals.length,
    merged_interval_count: merged.length,
    union_ms: unionMs,
    discussion_window_ms: windowMs,
    union_ratio: windowMs && windowMs > 0 ? Math.round((unionMs / windowMs) * 1000000) / 1000000 : null,
    baseline_union_ratio: 0.9704,
    target_reduction_ratio: 0.60,
    target_union_ratio: 0.35,
    meets_60_percent_reduction: windowMs && windowMs > 0
      ? unionMs / windowMs <= 0.9704 * 0.40
      : null,
    meets_35_percent_target: windowMs && windowMs > 0
      ? unionMs / windowMs < 0.35
      : null,
    intervals: merged.map(([start, end]) => ({
      acquired_at: new Date(start).toISOString(),
      released_at: new Date(end).toISOString(),
      duration_ms: end - start
    }))
  };
}

function buildP0Batch6Acceptance({ scenario, summary, baseline, finalSnapshot, events, errors }) {
  const pipelines = newRows(baseline, finalSnapshot, 'strategy_pipeline_runs');
  const emotionSlots = newRows(baseline, finalSnapshot, 'emotion_reflection_slots');
  const messages = newRows(baseline, finalSnapshot, 'messages');
  const latencyEvents = newRows(baseline, finalSnapshot, 'strategy_pipeline_latency_events');

  const hardMetrics = {
    preliminary_pipeline_with_lock: pipelines.filter((run) => (
      run.room_lock_acquired_at && !run.stage2_started_at
    )).length,
    consumerless_lock_count: pipelines.filter((run) => (
      run.room_lock_acquired_at && !run.stage2_started_at
    )).length,
    normal_terminal_without_release: pipelines.filter((run) => (
      run.room_lock_acquired_at && !run.room_lock_released_at &&
      isTerminal(run) && !containsLeaseExpiry(run)
    )).length,
    normal_test_TTL_recovery_count: pipelines.filter((run) => (
      containsLeaseExpiry(run) && !isFaultInjection(run)
    )).length,
    emotion_slot_permanently_skipped_by_strategy_lock: emotionSlots.filter((slot) => (
      upper(slot.status) === 'SKIPPED' && upper(slot.skip_reason) === 'STRATEGY_ROOM_LOCKED'
    )).length,
    duplicate_emotion_message: duplicateEmotionMessageCount(messages),
    self_regulation_detected_and_published: pipelines.filter((run) => (
      (Number(run.fresh_detected_self_regulation) === 1 || Number(run.detected_self_regulation) === 1) &&
      isPublished(run)
    )).length,
    OI_detected_and_published: pipelines.filter((run) => (
      (upper(run.inhibition_strategy_id).startsWith('OI-') ||
       upper(run.suppression_strategy_id).startsWith('OI-')) &&
      isPublished(run)
    )).length
  };
  const hardMetricsPassed = Object.values(hardMetrics).every((value) => value === 0);
  const emotionOutcome = {
    deferred_then_sent: emotionSlots.some((slot) => (
      Number(slot.defer_count || 0) > 0 && upper(slot.status) === 'SENT'
    )),
    intentionally_suppressed_after_strategy: emotionSlots.some((slot) => (
      upper(slot.status) === 'SUPPRESSED' && Boolean(slot.coordination_strategy_run_id)
    ))
  };
  emotionOutcome.passed = emotionOutcome.deferred_then_sent ||
    emotionOutcome.intentionally_suppressed_after_strategy;
  const strategyOutcome = {
    normal_intervention_published: pipelines.some((run) => (
      Number(run.should_intervene) === 1 && isPublished(run)
    )),
    oi_suppressed: pipelines.some((run) => (
      upper(run.inhibition_strategy_id).startsWith('OI-') && !isPublished(run)
    )),
    self_regulation_suppressed: pipelines.some((run) => (
      Number(run.fresh_detected_self_regulation) === 1 && !isPublished(run)
    )),
    failed_stage3_released: pipelines.some((run) => (
      upper(run.stage3_status) === 'FAILED' && run.room_lock_acquired_at && run.room_lock_released_at
    ))
  };

  return {
    schema_version: 'p0-batch6-acceptance/1',
    scenario: scenario.name,
    generated_at: new Date().toISOString(),
    audit_available: Boolean(summary.auditAvailable),
    hard_metrics: hardMetrics,
    hard_metrics_passed: hardMetricsPassed,
    lock_union: lockUnion(pipelines, messages),
    emotion_outcome: emotionOutcome,
    strategy_outcome: strategyOutcome,
    counts: {
      strategy_pipeline_runs: pipelines.length,
      emotion_reflection_slots: emotionSlots.length,
      latency_events: latencyEvents.length,
      student_and_agent_messages: messages.length,
      browser_events: events.length,
      browser_errors: errors.length
    },
    passed: Boolean(
      summary.auditAvailable && summary.actualServerCoveragePassed &&
      hardMetricsPassed && emotionOutcome.passed &&
      strategyOutcome.normal_intervention_published &&
      strategyOutcome.oi_suppressed && strategyOutcome.self_regulation_suppressed
    )
  };
}

function jsonLines(items) {
  return items.length ? `${items.map((item) => JSON.stringify(item)).join('\n')}\n` : '';
}

function writeP0Batch6Bundle({
  reportDir, runId, scenario, summary, events, errors, transcripts,
  strategyAuditSnapshots, acceptance
}) {
  const bundleDir = path.join(reportDir, runId);
  fs.mkdirSync(bundleDir, { recursive: true });
  const baseline = strategyAuditSnapshots.baseline || null;
  const finalSnapshot = strategyAuditSnapshots.final || null;
  const emotionSlots = newRows(baseline, finalSnapshot, 'emotion_reflection_slots');
  const pipelines = newRows(baseline, finalSnapshot, 'strategy_pipeline_runs');
  const latencyEvents = newRows(baseline, finalSnapshot, 'strategy_pipeline_latency_events');
  const roomLocks = (finalSnapshot && finalSnapshot.groups || []).map((group) => ({
    groupCode: group.groupCode,
    scope: group.dbAudit && group.dbAudit.scope,
    roomLock: group.dbAudit && group.dbAudit.room_lock
  }));
  const teacherApis = (finalSnapshot && finalSnapshot.groups || []).map((group) => ({
    groupCode: group.groupCode,
    teacherApis: group.teacherApis || null
  }));
  const strategyAudit = {
    runId,
    scenario: scenario.name,
    coverage: acceptance,
    snapshots: strategyAuditSnapshots
  };
  const files = {
    'summary.json': { ...summary, p0Batch6Acceptance: acceptance },
    'transcript.json': { runId, scenario: scenario.name, transcripts },
    'strategy-audit.json': strategyAudit,
    'emotion-slot-audit.json': { runId, slots: emotionSlots },
    'room-lock-audit.json': { runId, acceptance: acceptance.lock_union, roomLocks, pipelines },
    'three-stage-latency.json': { runId, events: latencyEvents },
    'teacher-api-snapshot.json': { runId, groups: teacherApis }
  };
  for (const [name, payload] of Object.entries(files)) {
    fs.writeFileSync(path.join(bundleDir, name), `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
  }
  fs.writeFileSync(path.join(bundleDir, 'events.jsonl'), jsonLines(events), 'utf8');
  fs.writeFileSync(path.join(bundleDir, 'errors.jsonl'), jsonLines(errors), 'utf8');
  return {
    bundleDir,
    files: [
      'summary.json', 'events.jsonl', 'errors.jsonl', 'transcript.json',
      'strategy-audit.json', 'emotion-slot-audit.json', 'room-lock-audit.json',
      'three-stage-latency.json', 'teacher-api-snapshot.json'
    ].map((name) => path.join(bundleDir, name))
  };
}

module.exports = {
  buildP0Batch6Acceptance,
  writeP0Batch6Bundle
};
