const MESSAGE_SEND_STATUS = Object.freeze({
  UI_INPUT_LOCKED: 'UI_INPUT_LOCKED',
  UI_FILL_FAILED: 'UI_FILL_FAILED',
  HTTP_423_LOCKED: 'HTTP_423_LOCKED',
  HTTP_OTHER_ERROR: 'HTTP_OTHER_ERROR',
  MESSAGE_NOT_VISIBLE: 'MESSAGE_NOT_VISIBLE',
  DATABASE_NOT_FOUND: 'DATABASE_NOT_FOUND',
  SUCCESS: 'SUCCESS'
});

function isComposerLocked(state = {}) {
  const ariaDisabled = String(
    state.ariaDisabled === undefined ? state.aria_disabled : state.ariaDisabled
  ).trim().toLowerCase();
  const className = String(state.className || state.class_name || '');
  return Boolean(
    state.locked ||
    state.readOnly ||
    state.readonly ||
    state.disabled ||
    ariaDisabled === 'true' ||
    /ai-locked/i.test(className)
  );
}

function messageDeliveryError(status, message, details = {}) {
  const error = new Error(message || status);
  error.deliveryStatus = status;
  Object.assign(error, details);
  return error;
}

function classifyMessageSendFailure(error, context = {}) {
  if (error && error.deliveryStatus) return error.deliveryStatus;

  const status = Number(
    context.status ||
    (error && (error.status || error.statusCode || error.httpStatus)) ||
    0
  );
  const code = String(
    context.code ||
    (error && (error.code || (error.body && error.body.code))) ||
    ''
  ).toUpperCase();
  const message = String(
    context.message || (error && error.message) || error || ''
  );

  if (context.stage === 'input_locked' || code === 'UI_INPUT_LOCKED') {
    return MESSAGE_SEND_STATUS.UI_INPUT_LOCKED;
  }
  if (context.stage === 'fill') return MESSAGE_SEND_STATUS.UI_FILL_FAILED;
  if (
    status === 423 ||
    code === 'ROOM_AI_INTERVENING' ||
    /ROOM_AI_INTERVENING|HTTP\s*423|STATUS\s*=\s*423/i.test(message)
  ) {
    return MESSAGE_SEND_STATUS.HTTP_423_LOCKED;
  }
  if (
    context.stage === 'visible' ||
    code === 'MESSAGE_NOT_VISIBLE' ||
    /message (?:did not become|not) visible/i.test(message)
  ) {
    return MESSAGE_SEND_STATUS.MESSAGE_NOT_VISIBLE;
  }
  if (
    status === 404 ||
    code.endsWith('_NOT_FOUND') ||
    /(database|group|room).*(not found|missing)/i.test(message)
  ) {
    return MESSAGE_SEND_STATUS.DATABASE_NOT_FOUND;
  }
  return MESSAGE_SEND_STATUS.HTTP_OTHER_ERROR;
}

function isRetryableMessageSendStatus(status) {
  return status === MESSAGE_SEND_STATUS.UI_INPUT_LOCKED ||
    status === MESSAGE_SEND_STATUS.HTTP_423_LOCKED;
}

function buildClientMessageId({ runId, studentId, messageIndex, kind = 'message' }) {
  const raw = `load-${runId}-${studentId}-${messageIndex}-${kind}`;
  return raw.replace(/[^A-Za-z0-9._-]+/g, '-').slice(0, 100);
}

function createMessageDeliveryRecord(message, now = () => new Date().toISOString()) {
  return {
    message_index: message.messageIndex ?? message.message_index ?? message.seq ?? null,
    client_message_id: message.clientMessageId || message.client_message_id || null,
    attempt_count: 0,
    first_attempt_at: null,
    last_attempt_at: null,
    final_status: null,
    attempts: [],
    _now: now
  };
}

function beginMessageDeliveryAttempt(record) {
  const at = record._now();
  record.attempt_count += 1;
  record.first_attempt_at = record.first_attempt_at || at;
  record.last_attempt_at = at;
  const attempt = {
    attempt_count: record.attempt_count,
    at,
    status: null
  };
  record.attempts.push(attempt);
  return attempt;
}

function completeMessageDeliveryAttempt(record, attempt, status, details = {}) {
  const at = record._now();
  record.last_attempt_at = at;
  attempt.status = status;
  attempt.completed_at = at;
  if (details.error) attempt.error = details.error;
  if (details.http_status !== undefined) attempt.http_status = details.http_status;
  if (details.message_id !== undefined) attempt.message_id = details.message_id;
  return attempt;
}

function finalizeMessageDelivery(record, status) {
  record.final_status = status;
  record.last_attempt_at = record._now();
  return serializeMessageDelivery(record);
}

function serializeMessageDelivery(record) {
  const { _now, ...publicRecord } = record;
  return {
    ...publicRecord,
    attempts: publicRecord.attempts.map((attempt) => ({ ...attempt }))
  };
}

function messageRetryBackoffMs(attemptCount, options = {}) {
  const baseMs = Math.max(0, Number(options.baseMs === undefined ? 250 : options.baseMs));
  const maxMs = Math.max(baseMs, Number(options.maxMs === undefined ? 5000 : options.maxMs));
  const exponent = Math.max(0, Number(attemptCount || 1) - 1);
  return Math.min(maxMs, baseMs * (2 ** exponent));
}

module.exports = {
  MESSAGE_SEND_STATUS,
  beginMessageDeliveryAttempt,
  buildClientMessageId,
  classifyMessageSendFailure,
  completeMessageDeliveryAttempt,
  createMessageDeliveryRecord,
  finalizeMessageDelivery,
  isComposerLocked,
  isRetryableMessageSendStatus,
  messageDeliveryError,
  messageRetryBackoffMs,
  serializeMessageDelivery
};
