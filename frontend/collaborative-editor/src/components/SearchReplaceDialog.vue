<template>
  <div v-if="visible" class="umo-sr-overlay" @mousedown.self="close">
    <div class="umo-sr-dialog" @mousedown.stop>
      <div class="umo-sr-header">
        <span class="umo-sr-title">Search &amp; Replace</span>
        <button class="umo-sr-close" @click="close" title="Close (Esc)">&times;</button>
      </div>

      <div class="umo-sr-body">
        <!-- Search input -->
        <div class="umo-sr-field">
          <label>Search</label>
          <div class="umo-sr-input-row">
            <input
              ref="searchInputRef"
              type="text"
              class="umo-sr-input"
              :value="searchText"
              placeholder="Search text..."
              @input="onSearchInput"
              @keydown.enter.prevent="nextResult"
            />
            <span class="umo-sr-count">
              {{ searchText ? currentIndex + 1 : 0 }}/{{ resultCount }}
            </span>
          </div>
        </div>

        <!-- Replace input -->
        <div class="umo-sr-field">
          <label>Replace with</label>
          <input
            type="text"
            class="umo-sr-input"
            :value="replaceText"
            placeholder="Replace text..."
            @input="onReplaceInput"
            @keydown.enter.prevent="replaceCurrent"
          />
        </div>

        <!-- Options -->
        <div class="umo-sr-options">
          <label class="umo-sr-checkbox-label">
            <input type="checkbox" :checked="caseSensitive" @change="onCaseSensitiveChange" />
            <span>Case sensitive</span>
          </label>
          <span v-if="isReadOnly" class="umo-sr-ro-badge">Read Only</span>
        </div>

        <!-- Buttons -->
        <div class="umo-sr-actions">
          <button class="umo-sr-btn" :disabled="resultCount === 0" @click="prevResult">
            &#x25B2; Prev
          </button>
          <button class="umo-sr-btn" :disabled="resultCount === 0" @click="nextResult">
            &#x25BC; Next
          </button>
          <button v-if="!isReadOnly" class="umo-sr-btn umo-sr-btn-primary" :disabled="resultCount === 0" @click="replaceCurrent">
            Replace
          </button>
          <button v-if="!isReadOnly" class="umo-sr-btn umo-sr-btn-primary" :disabled="resultCount === 0" @click="replaceAll">
            Replace All
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick } from "vue";

var props = defineProps({
  editor: { type: Object, default: null },
  visible: { type: Boolean, default: false },
  isReadOnly: { type: Boolean, default: false },
});

var emit = defineEmits(["close"]);

var searchInputRef = ref(null);
var searchText = ref("");
var replaceText = ref("");
var caseSensitive = ref(false);

// Sync from editor storage
var storage = computed(function () {
  return props.editor ? props.editor.storage.searchReplace : null;
});

var resultCount = computed(function () {
  return storage.value ? storage.value.results.length : 0;
});

var currentIndex = computed(function () {
  return storage.value ? storage.value.resultIndex : 0;
});

function onSearchInput(e) {
  searchText.value = e.target.value;
  if (props.editor && storage.value) {
    props.editor.commands.setSearchTerm(searchText.value);
    if (searchText.value) {
      props.editor.commands.resetIndex();
    }
  }
}

function onReplaceInput(e) {
  replaceText.value = e.target.value;
  if (props.editor && storage.value) {
    props.editor.commands.setReplaceTerm(replaceText.value);
  }
}

function onCaseSensitiveChange(e) {
  caseSensitive.value = e.target.checked;
  if (props.editor && storage.value) {
    props.editor.commands.setCaseSensitive(caseSensitive.value);
    if (searchText.value) {
      props.editor.commands.resetIndex();
    }
  }
}

function nextResult() {
  if (props.editor && storage.value && storage.value.results.length > 0) {
    props.editor.commands.nextSearchResult();
  }
}

function prevResult() {
  if (props.editor && storage.value && storage.value.results.length > 0) {
    props.editor.commands.previousSearchResult();
  }
}

function replaceCurrent() {
  if (props.editor && !props.isReadOnly && storage.value && storage.value.results.length > 0) {
    props.editor.commands.replaceCurrent();
    // After replace, recalculate
    props.editor.view.dispatch(props.editor.state.tr.setMeta("searchReplaceUpdate", true));
    props.editor.commands.focus();
  }
}

function replaceAll() {
  if (props.editor && !props.isReadOnly && storage.value && storage.value.results.length > 0) {
    props.editor.commands.replaceAll();
    props.editor.commands.focus();
  }
}

function close() {
  emit("close");
}

// Watch for visibility
watch(function () { return props.visible; }, function (val) {
  if (val) {
    nextTick(function () {
      if (searchInputRef.value) searchInputRef.value.focus();
    });
    // Populate from editor selection
    if (props.editor) {
      var sel = props.editor.state.selection;
      var txt = props.editor.state.doc.textBetween(sel.from, sel.to, " ", " ");
      if (txt && txt.length > 0 && txt.length < 500) {
        searchText.value = txt;
        props.editor.commands.setSearchTerm(txt);
        props.editor.commands.resetIndex();
      }
    }
  } else {
    // Clear search when closing
    if (props.editor && storage.value) {
      searchText.value = "";
      replaceText.value = "";
      props.editor.commands.setSearchTerm("");
      props.editor.commands.setReplaceTerm("");
      props.editor.commands.resetIndex();
    }
  }
});

/** Handle Escape key */
function onKeyDown(e) {
  if (e.key === "Escape" && props.visible) {
    close();
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("keydown", onKeyDown);
}
</script>

<style scoped>
.umo-sr-overlay {
  position: fixed;
  inset: 0;
  z-index: 2000;
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding-top: 80px;
  background: rgba(0,0,0,0.15);
}

.umo-sr-dialog {
  width: 400px;
  max-width: 90vw;
  background: #fff;
  border-radius: 12px;
  box-shadow: 0 8px 32px rgba(0,0,0,0.18);
  overflow: hidden;
}

.umo-sr-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px 8px;
  font-size: 14px;
  font-weight: 600;
  color: var(--ink, #333);
}

.umo-sr-close {
  width: 28px;
  height: 28px;
  border: none;
  background: transparent;
  font-size: 18px;
  cursor: pointer;
  border-radius: 6px;
  color: var(--ink-muted, #999);
}

.umo-sr-close:hover {
  background: var(--accent-dim, rgba(0,0,0,0.06));
}

.umo-sr-body {
  padding: 8px 16px 16px;
}

.umo-sr-field {
  margin-bottom: 10px;
}

.umo-sr-field label {
  display: block;
  font-size: 11px;
  font-weight: 600;
  color: var(--ink-2, #666);
  margin-bottom: 4px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
}

.umo-sr-input-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.umo-sr-input {
  flex: 1;
  padding: 8px 10px;
  border: 1px solid var(--line-soft, #ddd);
  border-radius: 6px;
  font-size: 13px;
  font-family: inherit;
  color: var(--ink, #333);
  outline: none;
  box-sizing: border-box;
  transition: border-color 0.15s;
}

.umo-sr-input:focus {
  border-color: var(--accent, #4a7c59);
}

.umo-sr-count {
  font-size: 11px;
  font-weight: 600;
  color: var(--ink-muted, #999);
  white-space: nowrap;
  min-width: 50px;
  text-align: center;
}

.umo-sr-options {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.umo-sr-checkbox-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--ink-2, #555);
  cursor: pointer;
}

.umo-sr-checkbox-label input[type="checkbox"] {
  width: 14px;
  height: 14px;
  cursor: pointer;
}

.umo-sr-ro-badge {
  font-size: 10px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 3px;
  background: #f3e5f5;
  color: #7b1fa2;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.umo-sr-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.umo-sr-btn {
  padding: 7px 14px;
  border: 1px solid var(--line-soft, #ddd);
  border-radius: 6px;
  font-size: 12px;
  font-family: inherit;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.12s;
  background: #fff;
  color: var(--ink-2, #555);
}

.umo-sr-btn:hover {
  background: var(--accent-dim, rgba(0,0,0,0.06));
}

.umo-sr-btn:disabled {
  opacity: 0.4;
  cursor: default;
}

.umo-sr-btn-primary {
  background: var(--accent, #4a7c59);
  color: #fff;
  border-color: var(--accent, #4a7c59);
}

.umo-sr-btn-primary:hover {
  opacity: 0.9;
}

.umo-sr-btn-primary:disabled {
  background: #aaa;
  border-color: #aaa;
}
</style>
