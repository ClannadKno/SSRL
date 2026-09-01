/**
 * useDocumentRecovery.js
 *
 * Handles document state recovery on page re-entry.
 * Queries the server to determine the correct state:
 * - Normal editing
 * - Being frozen / prepared
 * - Already submitted
 * - Needs recovery (prepared but no page refresh clean)
 */

import { ref, readonly } from "vue";

const SERVER_STATE = Object.freeze({
  EDITING: "editing",
  PREPARED: "prepared",
  SUBMITTED: "submitted",
  RETURNED: "returned",
  ERROR: "error",
});

export function useDocumentRecovery(config) {
  const { documentId, apiBase } = config;

  const serverState = ref(SERVER_STATE.EDITING);
  const recoveredData = ref(null);
  const recoveryError = ref(null);
  const recoveryDone = ref(false);

  async function recover() {
    recoveryError.value = null;
    recoveryDone.value = false;

    try {
      const metaRes = await fetch(apiBase + "/" + documentId);
      if (!metaRes.ok) {
        throw new Error("Failed to query document: HTTP " + metaRes.status);
      }
      const metaData = await metaRes.json();
      const meta = metaData.meta || metaData;
      const status = meta.status;

      if (status === "submitted" || status === "locked") {
        serverState.value = SERVER_STATE.SUBMITTED;
        recoveredData.value = meta;
        recoveryDone.value = true;
        return { action: "submit", meta };
      }

      if (status === "returned") {
        serverState.value = SERVER_STATE.RETURNED;
        recoveredData.value = meta;
        recoveryDone.value = true;
        return { action: "edit", meta };
      }

      try {
        const prepareRes = await fetch(apiBase + "/" + documentId + "/submit/prepare-status", {
          method: "GET",
          headers: { "Content-Type": "application/json" },
        });
        if (prepareRes.ok) {
          const prepareData = await prepareRes.json();
          if (prepareData.prepared && !prepareData.committed) {
            serverState.value = SERVER_STATE.PREPARED;
            recoveredData.value = { meta, ...prepareData };
            recoveryDone.value = true;
            return {
              action: "recover",
              meta,
              freezeInfo: {
                freeze_id: prepareData.freeze_id,
                state_revision: prepareData.state_revision,
              },
            };
          }
        }
      } catch (e) {
        console.warn("[Recovery] prepare-status query failed:", e.message);
      }

      serverState.value = SERVER_STATE.EDITING;
      recoveredData.value = meta;
      recoveryDone.value = true;
      return { action: "edit", meta };

    } catch (e) {
      serverState.value = SERVER_STATE.ERROR;
      recoveryError.value = e.message || String(e);
      recoveryDone.value = true;
      return { action: "error", error: recoveryError.value };
    }
  }

  return {
    serverState: readonly(serverState),
    recoveredData: readonly(recoveredData),
    recoveryError: readonly(recoveryError),
    recoveryDone: readonly(recoveryDone),
    recover,
    SERVER_STATE,
  };
}
