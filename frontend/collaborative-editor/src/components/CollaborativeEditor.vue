<template>
  <div ref="editorContainer" class="collab-editor-surface tiptap-editor-wrapper" :class="{ readonly: isReadOnly || isViewMode }">
    <div class="editor-top-bar">
      <OnlineMembers v-if="!isViewMode" :members="onlineMembers" :currentClientId="currentClientId" :compact="true" />
      <ConnectionStatus :status="connectionStatus" :syncStatus="syncStatus" :error="collabError" />
      <span v-if="isViewMode" class="conn-status view-only">Read Mode</span>
      <span v-if="docStatus" class="doc-status-badge" :class="'status-' + docStatus">{{ statusLabel }}</span>
      <span v-if="isViewMode && revision" class="revision-badge">rev {{ revision }}</span>
    </div>

    <!-- Connection warning banner -->
    <div v-if="showOfflineWarning" class="collab-offline-banner">
      <span class="offline-icon">&#x26A0;</span>
      <span class="offline-text">{{ offlineBannerText }}</span>
      <button v-if="connectionStatus === 'offline' || connectionStatus === 'error'" class="offline-retry-btn" @click="retryConnection">Reconnect</button>
    </div>

    <!-- Sync warning for local-only mode -->
    <div v-if="isLocalMode" class="collab-local-warning">
      <span class="local-icon">&#x26A0;</span>
      <span>Local editing mode - content is NOT synced and will be lost on refresh. Please wait for server connection.</span>
    </div>

    <!-- Toolbar -->
    <EditorToolbar v-if="!isViewMode" :editor="editorInstance" :isFormatPainterActive="formatPainterEnabled" :isWordWrapEnabled="wordWrapEnabled" @openLink="openLinkDialog" @openSearch="openSearch" @toggleFormatPainter="toggleFormatPainter" @toggleWordWrap="toggleWordWrap" />

    <!-- Editor content -->
    <div ref="editorRef" id="tiptap-editor"></div>

    <!-- Bubble Menu -->
    <EditorBubbleMenu v-if="!isViewMode" :editor="editorInstance" :linkDialogVisible="linkDialogVisible" @openLink="openLinkDialog" />

    <!-- Link Dialog -->
    <LinkDialog :visible="linkDialogVisible" :editor="editorInstance" @close="closeLinkDialog" />

    <!-- Footer (submit controls + save status) -->
    <div v-if="!isViewMode" class="editor-footer">
      <SaveStatus :status="saveStatus" />
      <SubmitControls @submit="handleSubmit" :disabled="!editorInstance || isReadOnly || isOffline || isSubmitted" :submitting="isSubmitting" :submitted="isSubmitted" />
    </div>

    <!-- Status Bar -->
    <StatusBar :charCount="charCount" :selCharCount="selCharCount" :connectionStatus="connectionStatus" :syncStatus="syncStatus" :lastSyncTime="lastSyncTime" :onlineCount="onlineCount" :onlineColors="onlineColors" :isReadOnly="isReadOnly || isViewMode" :docStatus="docStatus" :formatPainterActive="formatPainterEnabled" @cancelFormatPainter="cancelFormatPainter" />

    <!-- Teacher return controls -->
    <div v-if="isViewMode && docStatus === 'submitted'" class="return-controls">
      <button class="return-btn" @click="handleReturn" :disabled="returning">{{ returning ? "Submitting..." : "Return to Student" }}</button>
      <textarea v-model="returnReason" class="return-reason-input" placeholder="Reason for return (optional)" rows="2"></textarea>
    </div>
    <div v-if="isViewMode && docStatus === 'returned'" class="returned-notice">Returned - student can continue editing</div>

    <!-- Search & Replace Dialog -->
    <SearchReplaceDialog :editor="editorInstance" :visible="searchVisible" :isReadOnly="isReadOnly || isViewMode" @close="closeSearch" />

    <!-- Shortcut Panel -->
    <div v-if="shortcutVisible" class="umo-sp-overlay" @mousedown.self="shortcutVisible = false">
      <div class="umo-sp-drawer">
        <div class="umo-sp-drawer-header">
          <span class="umo-sp-drawer-title">Keyboard Shortcuts</span>
          <button class="umo-sp-drawer-close" @click="shortcutVisible = false">&times;</button>
        </div>
        <div class="umo-sp-drawer-content"><ShortcutPanel /></div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, shallowRef, inject, onMounted, onUnmounted, computed, watch } from "vue";
import EditorToolbar from "./EditorToolbar.vue";
import SaveStatus from "./SaveStatus.vue";
import SubmitControls from "./SubmitControls.vue";
import OnlineMembers from "./OnlineMembers.vue";
import ConnectionStatus from "./ConnectionStatus.vue";
import StatusBar from "./StatusBar.vue";
import EditorBubbleMenu from "./EditorBubbleMenu.vue";
import LinkDialog from "./LinkDialog.vue";
import SearchReplaceDialog from "./SearchReplaceDialog.vue";
import ShortcutPanel from "./ShortcutPanel.vue";
import { useDocumentState } from "../composables/useDocumentState.js";
import { useCollaboration } from "../composables/useCollaboration.js";
import { useSubmitStateMachine, SubmitState } from "../composables/useSubmitStateMachine.js";
import { useDocumentRecovery } from "../composables/useDocumentRecovery.js";
import { useToast } from "../composables/useToast.js";
import { createCharCountUpdater } from "../utils/charCount.js";

var props = defineProps({
  documentId: [String, Number],
  taskId: [String, Number],
  sessionNo: [String, Number],
  apiBase: String,
  wsUrl: String,
  displayName: String,
  permission: String,
  userId: [String, Number],
  userColor: String,
});

var emit = defineEmits(["submit-done"]);

var editorRef = ref(null);
var editorContainer = ref(null);
var editorInstance = shallowRef(null);
var isSubmitted = ref(false);
var docStatus = ref(null);
var revision = ref(0);
var returnReason = ref("");
var returning = ref(false);
var linkDialogVisible = ref(false);
var searchVisible = ref(false);
var shortcutVisible = ref(false);
var toast = useToast();
var charCount = ref(0);
var selCharCount = ref(0);

// Format painter state
var formatPainterEnabled = ref(false);
var wordWrapEnabled = ref(false);

var isViewMode = computed(function () { return props.permission === "view"; });
var isEditable = computed(function () { if (isViewMode.value) return false; if (!submitMachine) return false; return !submitMachine.isReadOnly.value; });

var isOffline = computed(function () {
  return connectionStatus.value === "offline" || connectionStatus.value === "error" || connectionStatus.value === "idle";
});

var isLocalMode = computed(function () {
  // local-yjs fallback: provider exists but not in collab mode
  return connectionStatus.value === "error" || connectionStatus.value === "offline";
});

var showOfflineWarning = computed(function () {
  if (isViewMode.value) return false;
  return connectionStatus.value === "offline" || connectionStatus.value === "error" || connectionStatus.value === "idle";
});

var offlineBannerText = computed(function () {
  if (connectionStatus.value === "connecting") return "Connecting to collaboration server...";
  if (connectionStatus.value === "idle") return "Not connected to collaboration server.";
  if (connectionStatus.value === "error") return "Connection error: " + (collabError.value || "unknown error");
  if (connectionStatus.value === "offline") return "Connection lost. Content may not be saved.";
  return "Connection issue";
});

var statusLabel = computed(function () {
  switch (docStatus.value) {
    case "editing": return "Editing"; case "submitted": return "Submitted"; case "returned": return "Returned"; case "locked": return "Locked"; default: return "";
  }
});

var { saveStatus, isReadOnly, isSubmitting, setReadOnly, setSubmitting, pollNonReactive } = useDocumentState();

var collabApi = inject("collabApi");

var { ydoc, fragment, connectionStatus, syncStatus, isSynced, collabError, lastSyncTime, onlineMembers, onlineCount, onlineColors, currentClientId, provider, awareness, connect: collabConnect, disconnect: collabDisconnect, destroy: collabDestroy, setTyping: collabSetTyping } = useCollaboration({
  documentId: props.documentId, apiBase: props.apiBase, wsUrl: props.wsUrl, displayName: props.displayName, userId: props.userId, userColor: props.userColor, permission: props.permission,
});

var submitMachine = null;
var pollCleanup = null;
var charCountUpdater = null;
var typingEnabled = false;

function onEditorUpdate() { if (charCountUpdater) charCountUpdater(); if (typingEnabled) collabSetTyping(true); }
function onSelectionUpdate() { if (charCountUpdater) charCountUpdater(); }

onMounted(async function () {
  if (!collabApi || !editorRef.value) return;
  if (isViewMode.value) {
    setReadOnly(true);
    var meta = await fetchDocMeta();
    if (meta && meta.status && meta.status !== "editing" && meta.status !== "returned") {
      viewMode.value = "historical";
      await loadTeacherState();
    } else {
      viewMode.value = "live";
      await collabConnect();
      if (ydoc.value && fragment.value) {
        try {
          collabApi.initEditor(editorRef.value, {
            ydoc: ydoc.value,
            provider: provider.value,
            fragment: fragment.value,
            mode: 'local-yjs',
          });
          editorInstance.value = collabApi.getEditor();
          editorInstance.value.setEditable(false);
          if (provider.value && provider.value.awareness) {
            provider.value.awareness.setLocalStateField("viewing", true);
            provider.value.awareness.setLocalStateField("user", { name: props.displayName || "Teacher", color: "#9b59b6", id: props.userId || 0, role: "teacher" });
          }
          charCountUpdater = createCharCountUpdater(charCount, selCharCount, function () { return editorInstance.value; }, 300);
          charCountUpdater();
        } catch (e) {
          console.error('[CollaborativeEditor] teacher live view initEditor error:', e);
          toast.error('Failed to load teacher live view');
        }
      }
    }
    return;
  }

  var initOk = false;
  try {
    await collabConnect();
  } catch (e) {
    console.error('[CollaborativeEditor] collabConnect threw:', e);
  }
  // Proceed if we have a Y.Doc (session exists) even if WebSocket failed
  if (ydoc.value && fragment.value) {
    try {
      var actualMode = provider.value ? 'collab' : 'local-yjs';
      collabApi.initEditor(editorRef.value, {
        ydoc: ydoc.value,
        provider: provider.value,
        fragment: fragment.value,
        mode: actualMode,
      });
      editorInstance.value = collabApi.getEditor();
      saveStatus.value = "saved";
      initOk = true;
    } catch (e) {
      console.error('[CollaborativeEditor] initEditor error:', e);
      toast.error('Failed to initialize collaborative editor. Please refresh or check the server.');
    }
  } else {
    // No session - create plain local editor (no Collaboration/CollaborationCaret)
    console.error('[CollaborativeEditor] no collaboration session, falling back to local editor');
    try {
      collabApi.initEditor(editorRef.value, {
        ydoc: null,
        provider: null,
        fragment: null,
        mode: 'plain',
      });
      editorInstance.value = collabApi.getEditor();
      saveStatus.value = "saved";
      initOk = true;
    } catch (e) {
      console.error('[CollaborativeEditor] local editor fallback error:', e);
      toast.error('Failed to initialize editor. Please refresh or check the server.');
    }
  }

  if (initOk) {
    charCountUpdater = createCharCountUpdater(charCount, selCharCount, function () { return editorInstance.value; }, 200);
    charCountUpdater();
    editorInstance.value.on("update", onEditorUpdate);
    editorInstance.value.on("selectionUpdate", onSelectionUpdate);

    setTimeout(function () { typingEnabled = true; }, 2000);

    submitMachine = useSubmitStateMachine({ documentId: props.documentId, apiBase: props.apiBase, collabApi: collabApi });

    // Restore state from server (handles already-submitted and hanging prepare)
    submitMachine.restoreFromServer().then(function(ok) {
      if (submitMachine.state.value === SubmitState.PREPARED) {
        // Hanging prepare detected - unfreeze room and reset state machine
        submitMachine.reset();
        fetch(props.apiBase + "/" + props.documentId + "/submit/unfreeze", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
        }).catch(function() {});
      } else if (submitMachine.state.value === SubmitState.SUBMITTED) {
        isSubmitted.value = true;
        docStatus.value = "submitted";
        setReadOnly(true);
        var ed = collabApi.getEditor();
        if (ed) ed.setEditable(false);
      }
    });
    pollCleanup = pollNonReactive(function () { return collabApi.getSaveStatus(); });

    // Add global keyboard listeners
    window.addEventListener("paste-image-blocked", onPasteImageBlocked);
    document.addEventListener("keydown", onGlobalKeyDown);
  }
});

onUnmounted(function () {
  if (typingEnabled) collabSetTyping(false);
  typingEnabled = false;
  if (editorInstance.value) {
    editorInstance.value.off("update", onEditorUpdate);
    editorInstance.value.off("selectionUpdate", onSelectionUpdate);
  }
  if (pollCleanup) pollCleanup();
  cancelFormatPainter();
  if (collabApi) collabApi.destroyEditorOnly();
  window.removeEventListener("paste-image-blocked", onPasteImageBlocked);
  document.removeEventListener("keydown", onGlobalKeyDown);
  editorInstance.value = null;
  collabDestroy();
});

// Link dialog
function openLinkDialog() { linkDialogVisible.value = true; }
function closeLinkDialog() { linkDialogVisible.value = false; if (editorInstance.value) editorInstance.value.view.focus(); }

// Search dialog
function openSearch() { searchVisible.value = true; }
function closeSearch() { searchVisible.value = false; if (editorInstance.value) editorInstance.value.view.focus(); }

// Format painter
function toggleFormatPainter() {
  if (!editorInstance.value) return;
  editorInstance.value.commands.toggleFormatPainter();
  formatPainterEnabled.value = !formatPainterEnabled.value;
}
function cancelFormatPainter() {
  if (!editorInstance.value) return;
  editorInstance.value.commands.unsetFormatPainter();
  formatPainterEnabled.value = false;
}

// Word wrap toggle
function toggleWordWrap() {
  if (!editorInstance.value) return;
  editorInstance.value.commands.toggleWordWrap();
  wordWrapEnabled.value = !wordWrapEnabled.value;
}

// Paste image blocked handler
function onPasteImageBlocked(e) {
  toast.warning(e.detail && e.detail.message ? e.detail.message : "Images cannot be pasted directly.");
}

// Global keyboard shortcuts
function onGlobalKeyDown(e) {
  // Ctrl/Cmd+F: Search
  if ((e.ctrlKey || e.metaKey) && e.key === "f") {
    if (editorInstance.value && editorInstance.value.view.hasFocus()) {
      e.preventDefault(); openSearch();
    }
  }
  // Ctrl/Cmd+A: Select all
  if ((e.ctrlKey || e.metaKey) && e.key === "a") {
    if (editorInstance.value && editorInstance.value.view.hasFocus()) {
      e.preventDefault(); editorInstance.value.commands.selectAll();
    }
  }
}

// --- Teacher mode (unchanged from batch 7) ---
var viewMode = ref("live");
async function fetchDocMeta() { try { var res = await fetch(props.apiBase + "/" + props.documentId); if (!res.ok) return null; return await res.json(); } catch (e) { return null; } }
async function loadTeacherState() {
  try {
    var res = await fetch(props.apiBase + "/" + props.documentId + "/teacher-state");
    if (!res.ok) throw new Error("HTTP " + res.status);
    var data = await res.json();
    docStatus.value = data.status;
    revision.value = data.state_revision;
    if (data.y_state_base64) {
      var Y = await import("yjs");
      var ydocLocal = new Y.Doc();
      var binaryStr = atob(data.y_state_base64);
      var binary = new Uint8Array(binaryStr.length);
      for (var i = 0; i < binaryStr.length; i++) binary[i] = binaryStr.charCodeAt(i);
      Y.applyUpdate(ydocLocal, binary);
      collabApi.initEditor(editorRef.value, {
        ydoc: ydocLocal,
        provider: null,
        fragment: ydocLocal.getXmlFragment("content"),
        mode: 'local-yjs',
      });
    } else { var ydocLocal = new (await import("yjs")).Doc();
      collabApi.initEditor(editorRef.value, {
        ydoc: ydocLocal,
        provider: null,
        fragment: ydocLocal.getXmlFragment("content"),
        mode: 'local-yjs',
      }); }
    editorInstance.value = collabApi.getEditor();
    editorInstance.value.setEditable(false);
    charCountUpdater = createCharCountUpdater(charCount, selCharCount, function () { return editorInstance.value; }, 300);
    charCountUpdater();
  } catch (e) { toast.error("Failed to load teacher view: " + (e.message || String(e))); }
}
async function handleReturn() { returning.value = true; try { var res = await fetch(props.apiBase + "/" + props.documentId + "/return", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason: returnReason.value || "" }) }); if (!res.ok) { var err = await res.json().catch(function () { return ({}); }); throw new Error(err.error || "HTTP " + res.status); } docStatus.value = "returned"; toast.success("Document returned successfully"); } catch (e) { toast.error("Return failed: " + (e.message || "Unknown error")); } finally { returning.value = false; } }
async function handleSubmit() { if (!collabApi) return; if (!submitMachine.canSubmit.value) return; setSubmitting(true); try { var providerRef = provider ? provider.value : null; var getContent = function () { var ed = collabApi.getEditor(); if (!ed) return {}; return { content_text: ed.getText(), content_html: ed.getHTML(), content_json: JSON.stringify(ed.getJSON()) }; }; var result = await submitMachine.submit({ provider: providerRef, getContent: getContent }); isSubmitted.value = true; docStatus.value = "submitted"; setReadOnly(true); toast.success("成果提交成功"); emit("submit-done", result); // Lock chat immediately via DOM
        var ci = document.getElementById("messageInput"); if (ci) ci.disabled = true;
        var sb = document.querySelector(".send-btn"); if (sb) sb.disabled = true;
        var hb = document.querySelector(".help-btn"); if (hb) hb.disabled = true;
        var params = new URLSearchParams(window.location.search);
        var tabToken = params.get("tab_token") || "";
        setTimeout(function() { window.location.href = "/student/collab?phase=posttest" + "&" + "tab_token=" + encodeURIComponent(tabToken); }, 500); } catch (e) { console.error("[Submit] Failed:", e); toast.error("提交失败: " + (e.message || "未知错误")); } finally { setSubmitting(false); } }

// Expose functions for toolbar/status bar communication
defineExpose({ openSearch, toggleFormatPainter, cancelFormatPainter, toggleWordWrap, formatPainterEnabled, wordWrapEnabled });
</script>
