class RunState {
  constructor({
    runId,
    totalStudents,
    minReadyStudents,
    groupPlans = {},
    scriptedMessages = []
  }) {
    this.runId = runId;
    this.totalStudents = totalStudents;
    this.minReadyStudents = minReadyStudents;
    this.groupPlans = groupPlans;
    this.enteredStudents = new Set();
    this.started = false;
    this.startedAt = null;
    this.endAt = null;
    this.stopRequested = false;
    this.stopReason = null;
    this.startWaiters = [];
    this.groupSubmitWaiters = new Map();
    this.groupTranscriptClaims = new Map();
    this.lastObservedInterventionAt = new Map();
    this.scriptedMessageQueues = this._buildScriptedMessageQueues(scriptedMessages);
    this.allScriptedMessageWaiters = [];
  }

  markEntered(studentId) {
    this.enteredStudents.add(studentId);
  }

  enteredCount() {
    return this.enteredStudents.size;
  }

  hasEntered(studentId) {
    return this.enteredStudents.has(studentId);
  }

  hasEnoughEntered() {
    return this.enteredStudents.size >= this.minReadyStudents;
  }

  waitUntilStarted() {
    if (this.started) return Promise.resolve();
    return new Promise((resolve) => {
      this.startWaiters.push(resolve);
    });
  }

  start(durationMs) {
    if (this.started) return;
    this.started = true;
    this.startedAt = Date.now();
    this.endAt = this.startedAt + durationMs;
    for (const resolve of this.startWaiters) resolve();
    this.startWaiters = [];
  }

  requestStop(reason) {
    if (!this.stopRequested) {
      this.stopRequested = true;
      this.stopReason = reason || 'stop requested';
      for (const queue of this.scriptedMessageQueues.values()) {
        for (const waiters of queue.waiters.values()) {
          for (const resolve of waiters) resolve(false);
        }
        queue.waiters.clear();
        for (const resolve of queue.drainWaiters) resolve(false);
        queue.drainWaiters = [];
      }
      for (const resolve of this.allScriptedMessageWaiters) resolve(false);
      this.allScriptedMessageWaiters = [];
    }
  }

  waitForScriptedMessageTurn(groupCode, sequence) {
    if (this.stopRequested) return Promise.resolve(false);
    const queue = this.scriptedMessageQueues.get(String(groupCode || ''));
    const seq = Number(sequence);
    if (!queue || !queue.positions.has(seq)) return Promise.resolve(true);
    if (queue.abandoned.has(seq)) return Promise.resolve(false);
    if (queue.positions.get(seq) < queue.nextIndex) return Promise.resolve(true);
    if (queue.sequences[queue.nextIndex] === seq) return Promise.resolve(true);
    return new Promise((resolve) => {
      const waiters = queue.waiters.get(seq) || [];
      waiters.push(resolve);
      queue.waiters.set(seq, waiters);
    });
  }

  completeScriptedMessageTurn(groupCode, sequence) {
    const queue = this.scriptedMessageQueues.get(String(groupCode || ''));
    const seq = Number(sequence);
    if (!queue || !queue.positions.has(seq)) return;
    queue.completed.add(seq);
    queue.lastCompletedAtMs = Date.now();
    this._advanceScriptedMessageQueue(queue);
  }

  abandonScriptedMessagesForStudent(studentId) {
    const owner = String(studentId || '');
    if (!owner) return [];
    const abandoned = [];
    for (const [groupCode, queue] of this.scriptedMessageQueues.entries()) {
      for (const sequence of queue.sequences) {
        if (
          queue.owners.get(sequence) !== owner ||
          queue.completed.has(sequence) ||
          queue.abandoned.has(sequence)
        ) {
          continue;
        }
        queue.abandoned.add(sequence);
        abandoned.push({ groupCode, sequence });
        const waiters = queue.waiters.get(sequence) || [];
        queue.waiters.delete(sequence);
        for (const resolve of waiters) resolve(false);
      }
      this._advanceScriptedMessageQueue(queue);
    }
    return abandoned;
  }

  _advanceScriptedMessageQueue(queue) {
    while (
      queue.nextIndex < queue.sequences.length &&
      (
        queue.completed.has(queue.sequences[queue.nextIndex]) ||
        queue.abandoned.has(queue.sequences[queue.nextIndex])
      )
    ) {
      queue.nextIndex += 1;
    }
    const nextSequence = queue.sequences[queue.nextIndex];
    const waiters = queue.waiters.get(nextSequence) || [];
    queue.waiters.delete(nextSequence);
    for (const resolve of waiters) resolve(!this.stopRequested);
    if (queue.nextIndex >= queue.sequences.length) {
      const complete = !this.stopRequested && queue.abandoned.size === 0;
      for (const resolve of queue.drainWaiters) resolve(complete);
      queue.drainWaiters = [];
    }
    if (this._allScriptedMessagesResolved()) {
      const complete = !this.stopRequested && this._allScriptedMessagesComplete();
      for (const resolve of this.allScriptedMessageWaiters) resolve(complete);
      this.allScriptedMessageWaiters = [];
    }
  }

  waitForScriptedMessagesComplete(groupCode) {
    if (this.stopRequested) return Promise.resolve(false);
    const queue = this.scriptedMessageQueues.get(String(groupCode || ''));
    if (!queue) return Promise.resolve(true);
    if (queue.nextIndex >= queue.sequences.length) {
      return Promise.resolve(queue.abandoned.size === 0);
    }
    return new Promise((resolve) => {
      queue.drainWaiters.push(resolve);
    });
  }

  waitForAllScriptedMessagesComplete() {
    if (this.stopRequested) return Promise.resolve(false);
    if (this._allScriptedMessagesResolved()) {
      return Promise.resolve(this._allScriptedMessagesComplete());
    }
    return new Promise((resolve) => {
      this.allScriptedMessageWaiters.push(resolve);
    });
  }

  finishDiscussionWindow() {
    if (!this.started || this.stopRequested) return false;
    this.endAt = Math.min(Number(this.endAt || Infinity), Date.now());
    return true;
  }

  recordObservedIntervention(groupCode, publishedAtMs = Date.now()) {
    const key = String(groupCode || '');
    const timestamp = Number(publishedAtMs);
    if (!key || !Number.isFinite(timestamp)) return;
    const current = Number(this.lastObservedInterventionAt.get(key) || 0);
    this.lastObservedInterventionAt.set(key, Math.max(current, timestamp));
  }

  async waitForPreviousInterventionGap(groupCode, minimumGapSeconds) {
    const key = String(groupCode || '');
    const minimumGapMs = Math.max(0, Number(minimumGapSeconds || 0) * 1000);
    const publishedAtMs = Number(this.lastObservedInterventionAt.get(key) || 0);
    if (!publishedAtMs || !minimumGapMs) return true;
    const remainingMs = publishedAtMs + minimumGapMs - Date.now();
    if (remainingMs <= 0) return true;
    await new Promise((resolve) => setTimeout(resolve, remainingMs));
    return !this.stopRequested;
  }

  async waitForScriptedMessageCadence(groupCode, minimumGapSeconds) {
    const queue = this.scriptedMessageQueues.get(String(groupCode || ''));
    const minimumGapMs = Math.max(0, Number(minimumGapSeconds || 0) * 1000);
    const completedAtMs = Number(queue && queue.lastCompletedAtMs || 0);
    if (!completedAtMs || !minimumGapMs) return !this.stopRequested;

    while (!this.stopRequested) {
      const remainingMs = completedAtMs + minimumGapMs - Date.now();
      if (remainingMs <= 0) return true;
      await new Promise((resolve) => setTimeout(resolve, Math.min(1000, remainingMs)));
    }
    return false;
  }

  shouldStop() {
    if (this.stopRequested) return true;
    if (!this.endAt) return false;
    return Date.now() >= this.endAt;
  }

  remainingMs() {
    if (!this.endAt) return null;
    return Math.max(0, this.endAt - Date.now());
  }

  claimDueGroupActions(groupCode, studentId) {
    if (!this.started || !groupCode) return [];
    const plan = this.groupPlans[groupCode];
    if (!plan || !Array.isArray(plan.actions)) return [];

    const elapsedMs = Date.now() - this.startedAt;
    const due = [];
    for (const action of plan.actions) {
      if (action.claimedBy || action.completed) continue;
      if (elapsedMs < action.offsetMs) continue;
      action.claimedBy = studentId;
      action.claimedAt = Date.now();
      due.push(action);
    }
    return due;
  }

  markGroupActionCompleted(groupCode, actionId, result = {}) {
    const action = this._findGroupAction(groupCode, actionId);
    if (!action) return;
    action.completed = true;
    action.completedAt = Date.now();
    action.result = result;
  }

  markGroupActionFailed(groupCode, actionId, error) {
    const action = this._findGroupAction(groupCode, actionId);
    if (!action) return;
    action.completed = true;
    action.failed = true;
    action.completedAt = Date.now();
    action.error = error && error.message ? error.message : String(error || 'unknown error');
  }

  claimGroupSubmission(groupCode, studentId) {
    const plan = this.groupPlans[groupCode];
    if (!plan || plan.submitted) return false;
    if (plan.submissionClaimedBy && plan.submissionClaimedBy !== studentId) return false;
    plan.submissionClaimedBy = studentId;
    return true;
  }

  releaseGroupSubmission(groupCode, studentId) {
    const plan = this.groupPlans[groupCode];
    if (plan && plan.submissionClaimedBy === studentId && !plan.submitted) {
      plan.submissionClaimedBy = null;
    }
  }

  markGroupSubmitted(groupCode, result = {}) {
    const plan = this.groupPlans[groupCode];
    if (!plan) return;
    plan.submitted = true;
    plan.submittedAt = Date.now();
    plan.submissionResult = result;
    const waiters = this.groupSubmitWaiters.get(groupCode) || [];
    for (const resolve of waiters) resolve(true);
    this.groupSubmitWaiters.delete(groupCode);
  }

  isGroupSubmitted(groupCode) {
    const plan = this.groupPlans[groupCode];
    return Boolean(plan && plan.submitted);
  }

  waitForGroupSubmitted(groupCode, timeoutMs) {
    if (this.isGroupSubmitted(groupCode)) return Promise.resolve(true);
    return new Promise((resolve) => {
      const waiters = this.groupSubmitWaiters.get(groupCode) || [];
      waiters.push(resolve);
      this.groupSubmitWaiters.set(groupCode, waiters);
      setTimeout(() => resolve(this.isGroupSubmitted(groupCode)), timeoutMs);
    });
  }

  claimGroupTranscript(groupCode, studentId) {
    const current = this.groupTranscriptClaims.get(groupCode);
    if (current && current.captured) return false;
    if (current && current.claimedBy && current.claimedBy !== studentId) return false;
    this.groupTranscriptClaims.set(groupCode, {
      claimedBy: studentId,
      claimedAt: Date.now(),
      captured: false
    });
    return true;
  }

  markGroupTranscriptCaptured(groupCode, result = {}) {
    const current = this.groupTranscriptClaims.get(groupCode) || {};
    this.groupTranscriptClaims.set(groupCode, {
      ...current,
      captured: true,
      capturedAt: Date.now(),
      result
    });
  }

  releaseGroupTranscript(groupCode, studentId) {
    const current = this.groupTranscriptClaims.get(groupCode);
    if (current && current.claimedBy === studentId && !current.captured) {
      this.groupTranscriptClaims.delete(groupCode);
    }
  }

  _findGroupAction(groupCode, actionId) {
    const plan = this.groupPlans[groupCode];
    if (!plan || !Array.isArray(plan.actions)) return null;
    return plan.actions.find((action) => action.id === actionId) || null;
  }

  _buildScriptedMessageQueues(messages) {
    const grouped = new Map();
    for (const message of messages || []) {
      const groupCode = String(message.groupCode || message.group_code || '');
      const sequence = Number(message.seq);
      if (!groupCode || !Number.isFinite(sequence)) continue;
      if (!grouped.has(groupCode)) grouped.set(groupCode, []);
      grouped.get(groupCode).push({
        sequence,
        owner: String(message.studentId || message.student_id || message.student || '')
      });
    }
    return new Map([...grouped.entries()].map(([groupCode, entries]) => {
      const owners = new Map();
      for (const entry of entries) {
        if (!owners.has(entry.sequence)) owners.set(entry.sequence, entry.owner);
      }
      const ordered = [...owners.keys()].sort((a, b) => a - b);
      return [groupCode, {
        sequences: ordered,
        positions: new Map(ordered.map((sequence, index) => [sequence, index])),
        owners,
        completed: new Set(),
        abandoned: new Set(),
        lastCompletedAtMs: 0,
        nextIndex: 0,
        waiters: new Map(),
        drainWaiters: []
      }];
    }));
  }

  _allScriptedMessagesComplete() {
    return [...this.scriptedMessageQueues.values()].every(
      (queue) => (
        queue.nextIndex >= queue.sequences.length &&
        queue.abandoned.size === 0
      )
    );
  }

  _allScriptedMessagesResolved() {
    return [...this.scriptedMessageQueues.values()].every(
      (queue) => queue.nextIndex >= queue.sequences.length
    );
  }
}

module.exports = {
  RunState
};
