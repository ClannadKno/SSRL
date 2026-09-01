import * as Y from "yjs";
import { WebsocketProvider } from "y-websocket";

// ---------------------------------------------------------------------------
// Connection status constants
// ---------------------------------------------------------------------------

export const ConnectionStatus = Object.freeze({
  IDLE: "idle",
  CONNECTING: "connecting",
  SYNCING: "syncing",
  CONNECTED: "connected",
  RECONNECTING: "reconnecting",
  OFFLINE: "offline",
  ERROR: "error",
  DESTROYED: "destroyed",
});

// ---------------------------------------------------------------------------
// Default color palette –stable assignment per user ID
// ---------------------------------------------------------------------------

var COLOR_PALETTE = [
  "#e74c3c", "#3498db", "#2ecc71", "#f39c12",
  "#9b59b6", "#1abc9c", "#e67e22", "#2980b9",
  "#d35400", "#27ae60", "#8e44ad", "#16a085",
];

/**
 * Deterministic color assignment based on user ID.
 * Same user ID always gets the same color.
 */
export function getColorForUser(userId) {
  if (userId == null) return COLOR_PALETTE[0];
  var hash = 0;
  var str = String(userId);
  for (var i = 0; i < str.length; i++) {
    hash = ((hash << 5) - hash + str.charCodeAt(i)) | 0;
  }
  var idx = Math.abs(hash) % COLOR_PALETTE.length;
  return COLOR_PALETTE[idx];
}

// ---------------------------------------------------------------------------
// Session factory
// ---------------------------------------------------------------------------

/**
 * Creates an independent collaboration session with Y.Doc, WebSocket Provider,
 * Awareness, and a finite state machine for connection status.
 *
 * @param {object} config
 * @param {string}  config.roomId       - Collaboration room identifier
 * @param {{name:string, color:string, id:number|string, role:string}} config.user
 * @param {string}  config.websocketUrl - WebSocket server URL
 * @param {string}  config.token        - Authentication token
 * @returns {SessionAPI}
 */
export function createCollaborationSession(config) {
  var roomId = config.roomId;
  var user = config.user;
  var websocketUrl = config.websocketUrl;
  var token = config.token;
  // [collab-diagnosis] Diagnostic logging helper
  function _diag() {
    if (typeof window !== "undefined" && window.__COLLAB_DIAG__) {
      var args = Array.prototype.slice.call(arguments);
      args.unshift("[collab-diagnosis]");
      console.log.apply(console, args);
    }
  }
  _diag("websocketUrl:", websocketUrl);
  _diag("roomId:", roomId);
  _diag("hasToken:", !!token);
  var userColor = (user.color && user.color !== "#666") ? user.color : getColorForUser(user.id);

  // ------------------------------------------------------------------
  // Internal state
  // ------------------------------------------------------------------

  var _status = ConnectionStatus.IDLE;
  var _statusListeners = new Set();

  var _ydoc = new Y.Doc();
  // Stable fragment for TipTap collaboration (v3 API)
  var _fragment = _ydoc.getXmlFragment("content");
  if (!_fragment || _fragment.doc !== _ydoc) {
    if (typeof window !== "undefined" && window.__COLLAB_DIAG__) {
      console.error("[collab-diagnosis] Fragment assertion failed");
    }
    throw new Error("[createCollaborationSession] Fragment init failed");
  }
  var _provider = new WebsocketProvider(websocketUrl, roomId, _ydoc, {
    params: { token: token },
    connect: false,
  });
  var _awareness = _provider.awareness;

  var _destroyed = false;
  var _eventsBound = false;
  var _syncedResolve = null;
  var _syncPromise = null;
  var _lastSyncTime = null;
  var _hasUnsyncedChanges = false;

  var _syncHandler = null;
  var _statusHandler = null;
  var _connectionErrorHandler = null;
  var _connectionCloseHandler = null;
  var _updateHandler = null;
  var _authFailHandler = null;

  // Typing state & throttling
  var _typingTimer = null;
  var _lastTypingBroadcast = 0;
  var _isTyping = false;
  var TYPING_THROTTLE_MS = 500;
  var TYPING_TIMEOUT_MS = 2000;

  // ------------------------------------------------------------------
  // Status helpers
  // ------------------------------------------------------------------

  function _setStatus(newStatus) {
    if (_destroyed) return;
    _status = newStatus;
    _statusListeners.forEach(function (fn) {
      try { fn(newStatus); } catch (e) { /* swallow */ }
    });
  }

  // ------------------------------------------------------------------
  // Sync status helpers
  // ------------------------------------------------------------------

  function _getSyncStatus() {
    if (_destroyed) return "destroyed";
    if (_status === ConnectionStatus.OFFLINE || _status === ConnectionStatus.ERROR) {
      return "offline";
    }
    if (_status === ConnectionStatus.CONNECTED || _status === ConnectionStatus.SYNCING) {
      return _hasUnsyncedChanges ? "unsynced" : "synced";
    }
    if (_status === ConnectionStatus.CONNECTING || _status === ConnectionStatus.RECONNECTING) {
      return "connecting";
    }
    return "idle";
  }

  // ------------------------------------------------------------------
  // Awareness helpers
  // ------------------------------------------------------------------

  function _buildAwarenessState() {
    return {
      user: {
        id: user.id,
        name: user.name || "User",
        color: userColor,
        role: user.role || "student",
      },
      isTyping: _isTyping,
      activeBlockId: null,
      viewing: true,
      updatedAt: Date.now(),
    };
  }

  function _setLocalAwareness() {
    if (_destroyed) return;
    try {
      _awareness.setLocalState(_buildAwarenessState());
    } catch (e) { /* ignore */ }
  }

  /**
   * Set typing state with throttling.
   * Only broadcasts to awareness at most every 500ms.
   * Auto-clears after 2000ms of inactivity.
   */
  function setTyping(typing) {
    if (_destroyed) return;
    var now = Date.now();

    if (typing) {
      _isTyping = true;
      if (_typingTimer) clearTimeout(_typingTimer);
      _typingTimer = setTimeout(function () {
        _isTyping = false;
        _setLocalAwareness();
      }, TYPING_TIMEOUT_MS);

      if (now - _lastTypingBroadcast >= TYPING_THROTTLE_MS) {
        _lastTypingBroadcast = now;
        _setLocalAwareness();
      }
    } else {
      if (_isTyping) {
        _isTyping = false;
        if (_typingTimer) clearTimeout(_typingTimer);
        _typingTimer = null;
        _setLocalAwareness();
      }
    }
  }

  // ------------------------------------------------------------------
  // Page visibility handling
  // ------------------------------------------------------------------

  function _handleVisibilityChange() {
    if (_destroyed) return;
    var hidden = document.hidden;
    try {
      _awareness.setLocalStateField("viewing", !hidden);
      if (hidden) {
        _awareness.setLocalStateField("isTyping", false);
        _isTyping = false;
        if (_typingTimer) {
          clearTimeout(_typingTimer);
          _typingTimer = null;
        }
      }
      _awareness.setLocalStateField("updatedAt", Date.now());
    } catch (e) { /* ignore */ }
  }

  function _bindVisibility() {
    document.addEventListener("visibilitychange", _handleVisibilityChange);
  }

  function _unbindVisibility() {
    document.removeEventListener("visibilitychange", _handleVisibilityChange);
  }

  // ------------------------------------------------------------------
  // Event binding
  // ------------------------------------------------------------------

  function _bindEvents() {
    if (_eventsBound) return;

    _syncHandler = function (synced) {
      if (_destroyed) return;
      _diag("provider sync:", synced);
      if (synced) {
        _lastSyncTime = Date.now();
        _hasUnsyncedChanges = false;
        _setStatus(ConnectionStatus.CONNECTED);
        if (_syncedResolve) {
          _syncedResolve();
          _syncedResolve = null;
          _syncPromise = null;
        }
      } else {
        _setStatus(ConnectionStatus.SYNCING);
      }
    };

    _statusHandler = function (event) {
      if (_destroyed) return;
      _diag("provider status:", event.status);
      switch (event.status) {
        case "connected":
          _setStatus(ConnectionStatus.CONNECTED);
          break;
        case "disconnected":
          if (_status !== ConnectionStatus.DESTROYED && _status !== ConnectionStatus.IDLE) {
            _setStatus(ConnectionStatus.OFFLINE);
          }
          break;
        case "connecting":
          _setStatus(ConnectionStatus.CONNECTING);
          break;
      }
    };

    _connectionErrorHandler = function () {
      if (_destroyed) return;
      _diag("connection-error");
      _setStatus(ConnectionStatus.ERROR);
      // Unblock waitUntilSynced() so connect() doesn't hang forever
      if (_syncedResolve) {
        _syncedResolve();
        _syncedResolve = null;
        _syncPromise = null;
      }
    };

    _connectionCloseHandler = function () {
      if (_destroyed) return;
      _diag("connection-close");
      // y-websocket emits (provider, closeEvent) to listeners
      var closeEvent = arguments.length >= 2 ? arguments[1] : (arguments[0] || null);
      var closeCode = (closeEvent && closeEvent.code) ? closeEvent.code : 0;
      if (closeCode === 4000 || closeCode === 4003 || closeCode === 4004) {
        _diag("auth-failure detected, code:", closeCode);
        if (_authFailHandler) {
          _authFailHandler(closeCode);
        }
      }
      if (_status !== ConnectionStatus.DESTROYED && _status !== ConnectionStatus.IDLE) {
        _setStatus(ConnectionStatus.OFFLINE);
      }
      // Unblock waitUntilSynced() so connect() completes even when connection drops
      if (_syncedResolve) {
        _syncedResolve();
        _syncedResolve = null;
        _syncPromise = null;
      }
    };

    _updateHandler = function () {
      if (_destroyed) return;
      _hasUnsyncedChanges = true;
    };

    _provider.on("sync", _syncHandler);
    _provider.on("status", _statusHandler);
    _provider.on("connection-error", _connectionErrorHandler);
    _provider.on("connection-close", _connectionCloseHandler);
    _ydoc.on("update", _updateHandler);

    _eventsBound = true;
  }

  function setAuthFailHandler(fn) {
    _authFailHandler = fn;
  }

  function refreshToken(newToken) {
    if (_destroyed) return;
    _diag("refreshing token...");

    // Unbind events from old provider
    if (_eventsBound) _unbindEvents();

    // Disconnect and destroy old provider
    try {
      _provider.disconnect();
      _provider.destroy();
    } catch (e) { /* ignore */ }

    // Create new provider with same Y.Doc but new token
    _provider = new WebsocketProvider(websocketUrl, roomId, _ydoc, {
      params: { token: newToken },
      connect: false,
    });
    _awareness = _provider.awareness;
    _setLocalAwareness();

    // Re-bind events
    _eventsBound = false;
    _bindEvents();

    // Reconnect
    _setStatus(ConnectionStatus.RECONNECTING);
    _provider.connect();
    _diag("token refreshed, reconnecting...");
  }

  function _unbindEvents() {
    if (!_eventsBound) return;

    _provider.off("sync", _syncHandler);
    _provider.off("status", _statusHandler);
    _provider.off("connection-error", _connectionErrorHandler);
    _provider.off("connection-close", _connectionCloseHandler);
    if (_updateHandler) {
      _ydoc.off("update", _updateHandler);
    }

    _syncHandler = null;
    _statusHandler = null;
    _connectionErrorHandler = null;
    _connectionCloseHandler = null;
    _updateHandler = null;
    _eventsBound = false;
  }

  // ------------------------------------------------------------------
  // Public API
  // ------------------------------------------------------------------

  function subscribe(fn) {
    _statusListeners.add(fn);
    return function () { _statusListeners.delete(fn); };
  }

  async function connect() {
    if (_destroyed) return;
    if (_eventsBound) return;

    _setStatus(ConnectionStatus.CONNECTING);

    try {
      _bindVisibility();
      _setLocalAwareness();
      _bindEvents();
      _provider.connect();
      await waitUntilSynced();
      // Verify actual status after sync completes
      if (_status !== ConnectionStatus.CONNECTED) {
        throw new Error("Sync failed, status: " + _status);
      }
    } catch (e) {
      _setStatus(ConnectionStatus.ERROR);
      console.error("[createCollaborationSession] connect error:", e);
      throw e;
    }
  }

  function waitUntilSynced() {
    if (_destroyed) return Promise.resolve();
    if (_status === ConnectionStatus.CONNECTED) return Promise.resolve();
    if (!_syncPromise) {
      _syncPromise = new Promise(function (resolve) { _syncedResolve = resolve; });
    }
    return _syncPromise;
  }

  function disconnect() {
    if (_destroyed) return;
    if (!_eventsBound) return;
    try { _awareness.setLocalState(null); } catch (e) { /* ignore */ }
    _provider.disconnect();
    _setStatus(ConnectionStatus.OFFLINE);
  }

  function reconnect() {
    if (_destroyed) return;
    if (!_eventsBound) return;
    _setStatus(ConnectionStatus.RECONNECTING);
    _setLocalAwareness();
    _provider.connect();
  }

  function destroy() {
    if (_destroyed) return;
    _destroyed = true;

    _unbindVisibility();
    _unbindEvents();
    if (_typingTimer) {
      clearTimeout(_typingTimer);
      _typingTimer = null;
    }
    try { _awareness.setLocalState(null); } catch (e) { /* ignore */ }
    try { _provider.destroy(); } catch (e) { /* ignore */ }
    try { _ydoc.destroy(); } catch (e) { /* ignore */ }

    _statusListeners.clear();
    _syncedResolve = null;
    _syncPromise = null;

    _setStatus(ConnectionStatus.DESTROYED);
  }

  return Object.freeze({
    get ydoc() { return _ydoc; },
    get fragment() { return _fragment; },
    get provider() { return _provider; },
    get awareness() { return _awareness; },
    get status() { return _status; },
    get isDestroyed() { return _destroyed; },
    get lastSyncTime() { return _lastSyncTime; },
    get hasUnsyncedChanges() { return _hasUnsyncedChanges; },
    get syncStatus() { return _getSyncStatus(); },
    subscribe: subscribe,
    connect: connect,
    disconnect: disconnect,
    reconnect: reconnect,
    waitUntilSynced: waitUntilSynced,
    destroy: destroy,
    setTyping: setTyping,
    setAuthFailHandler: setAuthFailHandler,
    refreshToken: refreshToken,
    getColorForUser: getColorForUser,
  });
}

