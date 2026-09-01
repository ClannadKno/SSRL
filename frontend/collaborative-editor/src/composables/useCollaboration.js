/**
 * useCollaboration.js (Batch 9 - connection hardening)
 *
 * Vue composable for collaboration session management.
 * Each call creates an independent session - no module-level globals.
 *
 * Connection state machine:
 *   idle -> requesting-ticket -> connecting-websocket -> syncing -> synced
 *                                                              |
 *                                                              v
 *                                                           failed
 *
 * Enhanced error categorization: auth failure, permission denied,
 * ticket timeout/server error, WebSocket failure, sync timeout.
 *
 * Batch 9 changes:
 * - Use fetchWithTimeout and requestTicketWithRetry from collaborativeApi.js
 * - Detailed connection states instead of generic "connecting"/"error"
 * - Clear error messages with user-facing suggestions
 * - Auth failure handler also uses requestTicketWithRetry
 */
import { ref, readonly, shallowRef } from "vue";
import { createCollaborationSession, ConnectionStatus } from "../services/createCollaborationSession.js";
import { fetchWithTimeout, requestTicketWithRetry } from "../services/collaborativeApi.js";

/**
 * @param {object} config
 * @param {string|number} config.documentId
 * @param {string}         config.apiBase
 * @param {string}         config.wsUrl
 * @param {string}         config.displayName
 * @param {string|number}  config.userId
 * @param {string}         config.userColor
 * @param {string}         config.permission  - "edit" or "view"
 */
export function useCollaboration(config) {
  // --- Reactive state ---
  var connectionStatus = ref("idle");
  var syncStatus = ref("idle");
  var isSynced = ref(false);
  var collabError = ref(null);
  var lastSyncTime = ref(null);
  var ydoc = shallowRef(null);
  var fragment = shallowRef(null);
  var onlineMembers = ref([]);
  var onlineCount = ref(0);
  var onlineColors = ref([]);
  var currentClientId = ref(null);
  var provider = shallowRef(null);
  var awareness = shallowRef(null);

  // --- Internal (non-reactive) ---
  var session = null;
  var ticketData = null;
  var unsubscribeStatus = null;
  var awarenessHandler = null;
  var syncTimer = null;

  // ------------------------------------------------------------------
  // Poll sync status from session (non-reactive getter)
  // ------------------------------------------------------------------

  function _pollSyncStatus() {
    if (syncTimer) clearInterval(syncTimer);
    syncTimer = setInterval(function () {
      if (!session || session.isDestroyed) {
        if (syncTimer) clearInterval(syncTimer);
        syncTimer = null;
        return;
      }
      lastSyncTime.value = session.lastSyncTime;
      syncStatus.value = session.syncStatus;
    }, 1000);
  }

  // ------------------------------------------------------------------
  // _mapSessionStatus - bridge session status to frontend status
  // ------------------------------------------------------------------

  function _mapSessionStatus(sessionStatus) {
    switch (sessionStatus) {
      case ConnectionStatus.CONNECTED:
        return "synced";
      case ConnectionStatus.SYNCING:
        return "syncing";
      case ConnectionStatus.CONNECTING:
      case ConnectionStatus.RECONNECTING:
        return "connecting-websocket";
      case ConnectionStatus.OFFLINE:
      case ConnectionStatus.ERROR:
        return "failed";
      case ConnectionStatus.IDLE:
      case ConnectionStatus.DESTROYED:
        return "idle";
      default:
        return "failed";
    }
  }

  // ------------------------------------------------------------------
  // connect
  // ------------------------------------------------------------------

  async function connect() {
    if (session && !session.isDestroyed) return;

    // Reset state
    connectionStatus.value = "requesting-ticket";
    syncStatus.value = "idle";
    collabError.value = null;
    isSynced.value = false;

    // Helper to get tab_token from URL
    function _getTabToken() {
      if (typeof window === "undefined") return "";
      return new URLSearchParams(window.location.search).get("tab_token") || "";
    }

    try {
      // 1. Fetch auth ticket with timeout + retry
      connectionStatus.value = "requesting-ticket";
      ticketData = await requestTicketWithRetry(
        config.apiBase,
        config.documentId,
        config.permission,
        _getTabToken(),
        3
      );

      // 2. Create independent session with full user info
      connectionStatus.value = "connecting-websocket";
      session = createCollaborationSession({
        roomId: "ws/doc-" + config.documentId,
        user: {
          name: config.displayName || "User",
          color: config.userColor || "#666",
          id: config.userId || 0,
          role: config.permission === "view" ? "teacher" : "student",
        },
        websocketUrl: config.wsUrl,
        token: ticketData.token,
      });

      // 2.5 Register auth failure handler to auto-refresh ticket
      session.setAuthFailHandler(async function() {
        console.warn("[useCollaboration] Auth failure detected, requesting new ticket...");
        connectionStatus.value = "requesting-ticket";
        try {
          var newTicketData = await requestTicketWithRetry(
            config.apiBase,
            config.documentId,
            config.permission,
            _getTabToken(),
            1  // Only 1 retry on auth refresh to avoid infinite loop
          );
          session.refreshToken(newTicketData.token);
          ticketData = newTicketData;
          connectionStatus.value = "connecting-websocket";
        } catch (e) {
          // Non-retryable auth errors or final failure
          if (e.code === "AUTH_FAILED" || e.code === "PERMISSION_DENIED") {
            collabError.value = e.message;
          } else {
            collabError.value = "认证续期失败，请刷新页面重试";
          }
          connectionStatus.value = "failed";
        }
      });

      // 3. Expose Y.Doc, provider, awareness
      ydoc.value = session.ydoc;
      fragment.value = session.fragment;
      provider.value = session.provider;
      awareness.value = session.awareness;
      currentClientId.value = session.awareness ? session.awareness.clientID : null;

      if (typeof window !== "undefined" && window.__COLLAB_DIAG__) {
        window.__COLLAB_DEBUG__ = {
          session: session,
          ydoc: session.ydoc,
          provider: session.provider,
          awareness: session.awareness,
        };
      }

      // 4. Subscribe to session status changes
      unsubscribeStatus = session.subscribe(function (status) {
        var mapped = _mapSessionStatus(status);
        connectionStatus.value = mapped;
        if (mapped === "synced") {
          isSynced.value = true;
        } else if (mapped === "failed") {
          // Only set error message if not already set by a more specific error
          if (!collabError.value) {
            if (status === ConnectionStatus.OFFLINE) {
              collabError.value = "协作连接已断开";
            } else {
              collabError.value = "协作连接失败";
            }
          }
          isSynced.value = false;
          syncStatus.value = "error";
        }
      });

      // 5. Listen for awareness changes with enhanced member info
      if (session.awareness) {
        awarenessHandler = function () {
          var states = session.awareness.getStates();
          var members = [];
          var colors = [];
          states.forEach(function (state, clientId) {
            if (state && state.user && state.viewing !== false) {
              var role = state.user.role || "student";
              members.push({
                clientId: clientId,
                name: state.user.name || "Anonymous",
                color: state.user.color || "#666",
                id: state.user.id,
                role: role,
                isTyping: state.isTyping === true,
                viewing: state.viewing !== false,
                activeBlockId: state.activeBlockId || null,
                updatedAt: state.updatedAt || 0,
                isLocal: clientId === currentClientId.value,
              });
              colors.push(state.user.color || "#666");
            }
          });
          members.sort(function (a, b) {
            if (a.isLocal !== b.isLocal) return a.isLocal ? 1 : -1;
            return (a.name || "").localeCompare(b.name || "");
          });
          onlineMembers.value = members;
          onlineCount.value = members.length;
          onlineColors.value = colors;
        };
        session.awareness.on("change", awarenessHandler);
        awarenessHandler();
      }

      // 6. Start sync status polling
      _pollSyncStatus();

      // 7. Connect WebSocket
      await session.connect();

    } catch (e) {
      // Categorize error for user-friendly display
      if (e.code === "AUTH_FAILED") {
        collabError.value = "认证失败，请重新登录";
      } else if (e.code === "PERMISSION_DENIED") {
        collabError.value = "权限验证未通过，请重新登录后再试";
      } else if (e.code === "TICKET_FAILED") {
        collabError.value = e.message || "连接失败，请稍后刷新页面";
      } else if (e.name === "RequestTimeoutError") {
        collabError.value = "服务器响应超时，请稍后刷新页面";
      } else if (e.message && e.message.indexOf("Sync failed") !== -1) {
        collabError.value = "协作连接同步失败，请稍后刷新页面";
      } else if (e.message && e.message.indexOf("NetworkError") !== -1) {
        collabError.value = "网络连接失败，请检查网络后刷新页面";
      } else {
        collabError.value = e.message ? e.message.split("\n")[0] : "连接失败，请刷新页面重试";
      }
      connectionStatus.value = "failed";
      syncStatus.value = "error";
      console.error("[useCollaboration] connect error:", e);
    }
  }

  // ------------------------------------------------------------------
  // disconnect / reconnect / waitUntilSynced
  // ------------------------------------------------------------------

  function disconnect() {
    if (session && !session.isDestroyed) {
      session.disconnect();
    }
  }

  function reconnect() {
    if (session && !session.isDestroyed) {
      session.reconnect();
    }
  }

  function waitUntilSynced() {
    return session ? session.waitUntilSynced() : Promise.resolve();
  }

  // ------------------------------------------------------------------
  // Typing
  // ------------------------------------------------------------------

  function setTyping(typing) {
    if (session && !session.isDestroyed) {
      session.setTyping(typing);
    }
  }

  // ------------------------------------------------------------------
  // destroy
  // ------------------------------------------------------------------

  function destroy() {
    if (syncTimer) {
      clearInterval(syncTimer);
      syncTimer = null;
    }

    if (awarenessHandler && session && session.awareness) {
      session.awareness.off("change", awarenessHandler);
    }
    awarenessHandler = null;

    if (unsubscribeStatus) {
      unsubscribeStatus();
    }
    unsubscribeStatus = null;

    if (session) {
      session.destroy();
    }
    session = null;

    ydoc.value = null;
    fragment.value = null;
    provider.value = null;
    awareness.value = null;
    currentClientId.value = null;
    ticketData = null;
    connectionStatus.value = "idle";
    syncStatus.value = "idle";
    isSynced.value = false;
    collabError.value = null;
    lastSyncTime.value = null;
    onlineMembers.value = [];
    onlineCount.value = 0;
    onlineColors.value = [];
  }

  // ------------------------------------------------------------------
  // Return
  // ------------------------------------------------------------------

  return {
    ydoc: readonly(ydoc),
    fragment: readonly(fragment),
    connectionStatus: readonly(connectionStatus),
    ydoc: ydoc,
    fragment: fragment,
    collabError: readonly(collabError),
    lastSyncTime: readonly(lastSyncTime),
    onlineMembers: readonly(onlineMembers),
    onlineCount: readonly(onlineCount),
    onlineColors: readonly(onlineColors),
    currentClientId: readonly(currentClientId),
    provider: provider,
    awareness: awareness,
    connect: connect,
    disconnect: disconnect,
    reconnect: reconnect,
    waitUntilSynced: waitUntilSynced,
    destroy: destroy,
    setTyping: setTyping,
    getTicketData: function () { return ticketData; },
    getSession: function () { return session; },
  };
}
