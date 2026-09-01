/**
 * main.js — Collaborative Editor entry point (Batch 8)
 *
 * Full rich-text formatting support:
 *   1-18. All batch 5 rich-text features
 * Batch 8: SearchReplace, FormatPainter, OfficePaste, WordWrap
 */

import { createApp, markRaw } from "vue";
import App from "./App.vue";
import "./styles/editor.css";

import * as Y from "yjs";
import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import Underline from "@tiptap/extension-underline";
import Link from "@tiptap/extension-link";
import { TextStyle, FontFamily, Color } from "@tiptap/extension-text-style";
import Highlight from "@tiptap/extension-highlight";
import TextAlign from "@tiptap/extension-text-align";
import { TaskList, TaskItem } from "@tiptap/extension-list";
import Collaboration from "@tiptap/extension-collaboration";
import CollaborationCaret from "@tiptap/extension-collaboration-caret";

import FontSize from "./extensions/FontSize.js";
import Indent from "./extensions/Indent.js";
import LineHeight from "./extensions/LineHeight.js";

import SearchReplace from "./extensions/SearchReplace.js";
import FormatPainter from "./extensions/FormatPainter.js";
import OfficePaste from "./extensions/OfficePaste.js";
import WordWrap from "./extensions/WordWrap.js";

// ---------------------------------------------------------------------------
// Config from page
// ---------------------------------------------------------------------------

const configScript = document.getElementById("collab-editor-config");
if (!configScript) {
  console.error("[CollabEditor] Missing #collab-editor-config script tag");
}
const config = configScript ? JSON.parse(configScript.textContent) : {};

const documentId = config.documentId;
const taskId = config.taskId;
const sessionNo = config.sessionNo;
const apiBase = config.apiBase || "/api/collaborative-documents";
const displayName = config.displayName || "User";
const permission = config.permission || "edit";
const userId = config.userId || 0;
const rawColor = config.userColor;
function _pickColor(id) {
  var colors = ["#e74c3c","#3498db","#2ecc71","#f39c12","#9b59b6","#1abc9c","#e67e22","#2980b9","#d35400","#27ae60","#8e44ad","#16a085"];
  if (id == null) return colors[0];
  var h = 0, s = String(id);
  for (var i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
  return colors[Math.abs(h) % colors.length];
}
const userColor = (rawColor && rawColor !== "#666") ? rawColor : _pickColor(userId);
const wsUrl = config.wsUrl || (function(){
  var proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return proto + "//" + window.location.hostname + ":8001";
})();

// ---------------------------------------------------------------------------
// Editor session state
// ---------------------------------------------------------------------------

let editor = null;
let _ownedYdoc = null;
let saveTimer = null;
let isDirty = false;
let saveStatus = "saved";

async function saveSnapshot() {
  if (!editor) return;
  const payload = {};
  payload.content_json = JSON.stringify(editor.getJSON());
  payload.content_html = editor.getHTML();
  payload.content_text = editor.getText();
  saveStatus = "saving";
  try {
    const res = await fetch(apiBase + "/" + documentId + "/snapshot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    isDirty = false;
    saveStatus = "saved";
  } catch (e) {
    saveStatus = "unsaved";
    console.error("[CollabEditor] saveSnapshot error:", e);
  }
}

function getFinalContentSnapshot(requestedDocumentId) {
  if (requestedDocumentId && Number(requestedDocumentId) !== Number(documentId)) return null;
  if (!editor) return null;
  return {
    content_json: JSON.stringify(editor.getJSON()),
    content_html: editor.getHTML(),
    content_text: editor.getText(),
  };
}

// The session shell owns timeout detection while this bundle owns the editor
// instance.  Expose a narrow read-only bridge so timeout submission can carry
// the latest display snapshot without allowing external mutation.
getFinalContentSnapshot.documentId = Number(documentId);
window.__COLLAB_EDITOR_GET_FINAL_CONTENT__ = getFinalContentSnapshot;

function markDirty() {
  if (!isDirty) {
    isDirty = true;
    saveStatus = "unsaved";
  }
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(saveSnapshot, 10000);
}

function flushBeforeUnload() {
  if (saveTimer) clearTimeout(saveTimer);
  if (!isDirty || !editor) return;
  const payload = JSON.stringify({
    content_json: JSON.stringify(editor.getJSON()),
    content_html: editor.getHTML(),
    content_text: editor.getText(),
  });
  navigator.sendBeacon(
    apiBase + "/" + documentId + "/snapshot",
    new Blob([payload], { type: "application/json" })
  );
}

// ---------------------------------------------------------------------------
// Clear format command
// ---------------------------------------------------------------------------

const clearFormatCommand = ({ tr, dispatch, state }) => {
  const { from, to } = state.selection;
  const doc = state.doc;
  const marks = Object.values(state.schema.marks);
  marks.forEach((mark) => { tr.removeMark(from, to, mark); });
  const nodeTypes = ["paragraph", "heading"];
  doc.nodesBetween(from, to, (node, pos) => {
    if (!nodeTypes.includes(node.type.name)) return;
    const newAttrs = { ...node.attrs };
    delete newAttrs.textAlign;
    delete newAttrs.lineHeight;
    tr = tr.setNodeMarkup(pos, undefined, newAttrs);
  });
  if (tr.docChanged && dispatch) { dispatch(tr); }
  return true;
};

// ---------------------------------------------------------------------------
// Editor initializer
// ---------------------------------------------------------------------------

function initEditor(element, options = {}) {
  const {
    mode = 'collab',
    ydoc: ydocInstance,
    provider: providerInstance,
    fragment: fragmentInstance,
  } = options;

  // 1. Only destroy the previous editor - never touch the callers ydoc/provider.
  destroyEditorOnly();

  // 2. Resolve ydoc and fragment
  const ydoc = ydocInstance || new Y.Doc();
  const fragment = fragmentInstance || ydoc.getXmlFragment('content');

  // 3. Fragment validation
  if (!fragment) {
    throw new Error('[editor] Could not resolve Y.XmlFragment');
  }
  if (!fragment.doc) {
    throw new Error('[editor] Invalid Y.XmlFragment: fragment.doc is null - the Y.Doc may have been destroyed');
  }
  if (fragment.doc !== ydoc) {
    throw new Error('[editor] Y.XmlFragment does not belong to the provided Y.Doc');
  }

  // 4. Mode validation
  if (mode === 'collab' && !providerInstance) {
    throw new Error('[editor] collab mode requires a provider instance');
  }

  // 5. Build extension list
  const extensions = [
    StarterKit.configure({ undoRedo: false, link: false, underline: false }),
    Underline,
    TextStyle,
    FontFamily,
    Color.configure({ types: ['textStyle'] }),
    FontSize,
    Highlight.configure({ multicolor: false }),
    TextAlign.configure({
      types: ['heading', 'paragraph'],
      alignments: ['left', 'center', 'right', 'justify'],
      defaultAlignment: 'left',
    }),
    TaskList.configure({ HTMLAttributes: { class: 'umo-task-list' } }),
    TaskItem.configure({ nested: true, HTMLAttributes: { class: 'umo-task-item' } }),
    Indent,
    LineHeight,
    Link.configure({ openOnClick: true }),
    SearchReplace.configure(),
    FormatPainter.configure(),
    OfficePaste.configure(),
    WordWrap.configure(),
  ];

  // Collaboration extension for Yjs-backed modes
  if (mode !== 'plain') {
    extensions.push(Collaboration.configure({ fragment }));
  }

  // CollaborationCaret only for live collab with a provider
  if (mode === 'collab' && providerInstance) {
    extensions.push(CollaborationCaret.configure({
      provider: providerInstance,
      user: { name: displayName, color: userColor },
    }));
  }

  // 6. Track owned ydoc (only when initEditor creates its own Y.Doc)
  _ownedYdoc = ydocInstance ? null : ydoc;

  // 7. Create the editor
  editor = markRaw(new Editor({
    element,
    extensions,
    editorProps: {
      attributes: { class: 'tiptap-content' },
    },
    onUpdate() { markDirty(); },
    onSelectionUpdate() {},
  }));
}

// Public API for toolbar
Editor.prototype.execClearFormat = function () {
  this.chain().focus().command(clearFormatCommand).run();
};

Editor.prototype.execTurnToParagraph = function () {
  this.chain().focus().clearNodes().unsetAllMarks().run();
};

Editor.prototype.execSelectAll = function () {
  this.commands.selectAll();
};

Editor.prototype.execToggleWordWrap = function () {
  this.commands.toggleWordWrap();
};

function destroyEditorOnly() {
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
  if (editor) {
    try { editor.destroy(); } catch (e) { /* safe to ignore during cleanup */ }
    editor = null;
  }
  isDirty = false;
  saveStatus = "saved";
}

function destroyCollaborationSession() {
  destroyEditorOnly();
  if (window.__COLLAB_EDITOR_GET_FINAL_CONTENT__ === getFinalContentSnapshot) {
    delete window.__COLLAB_EDITOR_GET_FINAL_CONTENT__;
  }
  if (_ownedYdoc) {
    try { _ownedYdoc.destroy(); } catch (e) { /* ignore */ }
    _ownedYdoc = null;
  }
}

// ---------------------------------------------------------------------------
// Collab API
// ---------------------------------------------------------------------------

const collabApi = {
  documentId, taskId, sessionNo, apiBase, wsUrl,
  displayName, permission, userId, userColor,
  initEditor,
  destroyEditorOnly,
  destroyCollaborationSession,
  destroyEditor: destroyEditorOnly,
  saveSnapshot, markDirty, getFinalContentSnapshot,
  getEditor: () => editor,
  getSaveStatus: () => saveStatus,
  getIsDirty: () => isDirty,
};

// ---------------------------------------------------------------------------
// Bootstrap Vue app
// ---------------------------------------------------------------------------

const app = createApp(App, {
  documentId, taskId, sessionNo, apiBase, wsUrl,
  displayName, permission, userId, userColor,
});

app.provide("collabApi", collabApi);
window.addEventListener("beforeunload", flushBeforeUnload);
app.mount("#collaborative-editor-app");
