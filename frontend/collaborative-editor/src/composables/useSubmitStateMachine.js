/**
 * useSubmitStateMachine.js
 *
 * Centralized submit state machine for the two-phase commit workflow.
 *
 * States:
 *   idle       - No submission in progress
 *   syncing    - Waiting for Yjs provider sync before prepare
 *   preparing  - Calling prepare API (freeze + flush)
 *   prepared   - Room is frozen, ticket received, ready to commit
 *   committing - Calling commit API (submit + close)
 *   submitted  - Successful submission, editor is read-only
 *   recovering - Commit failed, attempting safe recovery (unfreeze)
 *   error      - Non-recoverable error, user can retry
 *
 * Each call returns an independent state machine.
 * The component that creates it owns its lifecycle.
 */

import { ref, readonly, computed } from "vue";

// ---------------------------------------------------------------------------
// State constants
// ---------------------------------------------------------------------------

export const SubmitState = Object.freeze({
  IDLE: "idle",
  SYNCING: "syncing",
  PREPARING: "preparing",
  PREPARED: "prepared",
  COMMITTING: "committing",
  SUBMITTED: "submitted",
  RECOVERING: "recovering",
  ERROR: "error",
});

// ---------------------------------------------------------------------------
// Timeout constants (milliseconds)
// ---------------------------------------------------------------------------

const PREPARE_TIMEOUT = 15000;
const COMMIT_TIMEOUT = 15000;
const SYNC_TIMEOUT = 10000;

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

/**
 * @param {object} config
 * @param {string|number} config.documentId
 * @param {string}         config.apiBase
 * @param {object}         config.collabApi
 */
export function useSubmitStateMachine(config) {
  const { documentId, apiBase, collabApi } = config;

  // ------------------------------------------------------------------
  // Reactive state (single source of truth)
  // ------------------------------------------------------------------

  const _state = ref(SubmitState.IDLE);
  const _error = ref(null);
  const _result = ref(null);
  const _freezeInfo = ref(null);
  const _submittedAt = ref(null);
  const _isReadOnly = ref(false);

  const isBusy = computed(() => {
    const s = _state.value;
    return s === SubmitState.SYNCING
      || s === SubmitState.PREPARING
      || s === SubmitState.COMMITTING
      || s === SubmitState.RECOVERING;
  });

  const canSubmit = computed(() => {
    return _state.value === SubmitState.IDLE
      || _state.value === SubmitState.ERROR;
  });

  // ------------------------------------------------------------------
  // Helpers
  // ------------------------------------------------------------------

  function _setState(s) {
    _state.value = s;
    if (s === SubmitState.SUBMITTED) {
      _isReadOnly.value = true;
      _submittedAt.value = new Date().toISOString();
    }
    if (s === SubmitState.IDLE) {
      _isReadOnly.value = false;
    }
  }

  function _sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  function _withTimeout(promise, ms, label) {
    let timer;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(label + " timed out after " + ms + "ms")), ms);
    });
    return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
  }

  // ------------------------------------------------------------------
  // Status query helpers (for timeout recovery)
  // ------------------------------------------------------------------

  async function _queryPrepareStatus() {
    try {
      const res = await fetch(apiBase + "/" + documentId + "/submit/prepare-status", {
        method: "GET",
        headers: { "Content-Type": "application/json" },
      });
      if (!res.ok) return null;
      return await res.json();
    } catch (e) {
      return null;
    }
  }

  async function _queryCommitStatus() {
    try {
      const res = await fetch(apiBase + "/" + documentId + "/submit/commit-status", {
        method: "GET",
        headers: { "Content-Type": "application/json" },
      });
      if (!res.ok) return null;
      return await res.json();
    } catch (e) {
      return null;
    }
  }

  async function _queryServerStatus() {
    try {
      const metaRes = await fetch(apiBase + "/" + documentId);
      if (!metaRes.ok) return null;
      const data = await metaRes.json();
      return data.meta || data;
    } catch (e) {
      return null;
    }
  }

  // ------------------------------------------------------------------
  // Core: wait for Yjs sync
  // ------------------------------------------------------------------

  async function _waitForSync(provider) {
    if (!provider) return;
    _setState(SubmitState.SYNCING);
    try {
      await _withTimeout(
        new Promise((resolve) => {
          if (provider.synced) return resolve();
          const onSync = (synced) => {
            if (synced) {
              provider.off("sync", onSync);
              resolve();
            }
          };
          provider.on("sync", onSync);
        }),
        SYNC_TIMEOUT,
        "Sync"
      );
    } catch (e) {
      console.warn("[SubmitSM] Sync timeout, continuing:", e.message);
    }
  }

  // ------------------------------------------------------------------
  // Main: submit entry point
  // ------------------------------------------------------------------

  async function submit(deps) {
    const { provider, getContent } = deps;

    if (!canSubmit.value) {
      throw new Error("Submit already in progress or already submitted");
    }

    _error.value = null;
    _result.value = null;

    try {
      // Phase 0: Wait for Yjs sync
      await _waitForSync(provider);

      // Phase 1: Prepare (freeze + flush)
      _setState(SubmitState.PREPARING);

      const prepareRes = await _withTimeout(
        fetch(apiBase + "/" + documentId + "/submit/prepare", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
        }).then(async (r) => {
          if (!r.ok) {
            const errData = await r.json().catch(() => ({ error: "HTTP " + r.status }));
            throw new Error(errData.error || "Prepare failed with HTTP " + r.status);
          }
          return r.json();
        }),
        PREPARE_TIMEOUT,
        "Prepare"
      );

      _freezeInfo.value = {
        freeze_id: prepareRes.freeze_id,
        state_revision: prepareRes.state_revision,
      };
      _setState(SubmitState.PREPARED);

      // Phase 2: Commit (submit + close)
      _setState(SubmitState.COMMITTING);

      const content = getContent ? getContent() : {};
      const commitPayload = {
        freeze_id: _freezeInfo.value.freeze_id,
        state_revision: _freezeInfo.value.state_revision,
        content_text: (content.content_text || ""),
        content_html: (content.content_html || ""),
        content_json: (content.content_json || JSON.stringify({})),
      };

      const commitRes = await _withTimeout(
        fetch(apiBase + "/" + documentId + "/submit/commit", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(commitPayload),
        }).then(async (r) => {
          if (r.status === 409) {
            return { ok: true, status: "submitted", already_submitted: true };
          }
          if (!r.ok) {
            const errData = await r.json().catch(() => ({ error: "HTTP " + r.status }));
            throw new Error(errData.error || "Commit failed with HTTP " + r.status);
          }
          return r.json();
        }),
        COMMIT_TIMEOUT,
        "Commit"
      );

      // Success
      _result.value = commitRes;
      _setState(SubmitState.SUBMITTED);
      if (collabApi) {
        const editor = collabApi.getEditor();
        if (editor) editor.setEditable(false);
      }
      return commitRes;

    } catch (e) {
      const isTimeout = e.message && e.message.includes("timed out");
      const currentState = _state.value;

      if (isTimeout) {
        const resolved = await _resolveTimeout(currentState);
        if (resolved) {
          return _result.value;
        }
      }

      if (currentState === SubmitState.COMMITTING) {
        await _recoverAfterFailedCommit();
        throw e;
      }

      if (currentState === SubmitState.PREPARING || currentState === SubmitState.PREPARED) {
        await _unfreezeIfNeeded();
      }

      _error.value = e.message || String(e);
      _setState(SubmitState.ERROR);
      throw e;
    }
  }

  // ------------------------------------------------------------------
  // Timeout resolution
  // ------------------------------------------------------------------

  async function _resolveTimeout(stage) {
    await _sleep(2000);

    if (stage === SubmitState.PREPARING) {
      const status = await _queryPrepareStatus();
      if (status && status.prepared && status.freeze_id) {
        _freezeInfo.value = { freeze_id: status.freeze_id, state_revision: status.state_revision };
        _setState(SubmitState.PREPARED);
        _result.value = status;
        return true;
      }
      const meta = await _queryServerStatus();
      if (meta && (meta.status === "submitted" || meta.status === "locked")) {
        _setState(SubmitState.SUBMITTED);
        _result.value = { status: "submitted" };
        return true;
      }
      await _unfreezeIfNeeded();
      return false;
    }

    if (stage === SubmitState.COMMITTING) {
      const status = await _queryCommitStatus();
      if (status && status.submitted) {
        _setState(SubmitState.SUBMITTED);
        _result.value = status;
        return true;
      }
      const meta = await _queryServerStatus();
      if (meta && (meta.status === "submitted" || meta.status === "locked")) {
        _setState(SubmitState.SUBMITTED);
        _result.value = { status: "submitted" };
        return true;
      }
      await _recoverAfterFailedCommit();
      return false;
    }

    return false;
  }

  // ------------------------------------------------------------------
  // Safe recovery
  // ------------------------------------------------------------------

  async function _recoverAfterFailedCommit() {
    _setState(SubmitState.RECOVERING);
    try {
      const commitStatus = await _queryCommitStatus();
      if (commitStatus && commitStatus.submitted) {
        _setState(SubmitState.SUBMITTED);
        _result.value = commitStatus;
        return true;
      }
      const meta = await _queryServerStatus();
      if (meta && (meta.status === "submitted" || meta.status === "locked")) {
        _setState(SubmitState.SUBMITTED);
        _result.value = meta;
        return true;
      }

      await _callUnfreeze();
      _setState(SubmitState.IDLE);
      return false;
    } catch (recoveryError) {
      console.error("[SubmitSM] Recovery failed:", recoveryError);
      _error.value = "Recovery failed: " + (recoveryError.message || String(recoveryError));
      _setState(SubmitState.ERROR);
      return false;
    }
  }

  async function _unfreezeIfNeeded() {
    try {
      await _callUnfreeze();
    } catch (e) {
      console.warn("[SubmitSM] Unfreeze failed:", e.message);
    }
  }

  async function _callUnfreeze() {
    const res = await fetch(apiBase + "/" + documentId + "/submit/unfreeze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.error || "Unfreeze failed: HTTP " + res.status);
    }
  }

  // ------------------------------------------------------------------
  // Reset
  // ------------------------------------------------------------------

  function reset() {
    _state.value = SubmitState.IDLE;
    _error.value = null;
    _result.value = null;
    _freezeInfo.value = null;
    _isReadOnly.value = false;
  }

  // ------------------------------------------------------------------
  // Restore state from server (page re-entry)
  // ------------------------------------------------------------------

  async function restoreFromServer() {
    try {
      const meta = await _queryServerStatus();
      if (!meta) return false;

      const status = meta.status;
      if (status === "submitted" || status === "locked") {
        _setState(SubmitState.SUBMITTED);
        return true;
      }

      const prepareStatus = await _queryPrepareStatus();
      if (prepareStatus && prepareStatus.prepared && !prepareStatus.committed) {
        _freezeInfo.value = { freeze_id: prepareStatus.freeze_id, state_revision: prepareStatus.state_revision };
        _setState(SubmitState.PREPARED);
        return true;
      }

      _setState(SubmitState.IDLE);
      return true;
    } catch (e) {
      console.warn("[SubmitSM] Restore failed:", e.message);
      _setState(SubmitState.IDLE);
      return false;
    }
  }

  return {
    state: readonly(_state),
    error: readonly(_error),
    result: readonly(_result),
    freezeInfo: readonly(_freezeInfo),
    submittedAt: readonly(_submittedAt),
    isReadOnly: readonly(_isReadOnly),
    isBusy,
    canSubmit,
    submit,
    reset,
    restoreFromServer,
  };
}
