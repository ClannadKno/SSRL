<template>
  <div
    v-if="visible"
    ref="bubbleRef"
    class="umo-bubble-menu"
    :class="{ 'umo-bubble-menu-in-code': isInCodeBlock }"
    @mousedown.prevent
  >
    <!-- In code blocks: limited options -->
    <template v-if="isInCodeBlock">
      <button class="umo-bm-btn" :class="{ active: editor.isActive('bold') }" title="Bold (Ctrl+B)" @click="exec('toggleBold')"><b>B</b></button>
      <button class="umo-bm-btn" :class="{ active: editor.isActive('italic') }" title="Italic (Ctrl+I)" @click="exec('toggleItalic')"><i>I</i></button>
      <button class="umo-bm-btn" :class="{ active: editor.isActive('strike') }" title="Strikethrough" @click="exec('toggleStrike')"><s>S</s></button>
    </template>
    <template v-else>
      <button class="umo-bm-btn" :class="{ active: editor.isActive('bold') }" title="Bold (Ctrl+B)" @click="exec('toggleBold')"><b>B</b></button>
      <button class="umo-bm-btn" :class="{ active: editor.isActive('italic') }" title="Italic (Ctrl+I)" @click="exec('toggleItalic')"><i>I</i></button>
      <button class="umo-bm-btn" :class="{ active: editor.isActive('underline') }" title="Underline (Ctrl+U)" @click="exec('toggleUnderline')"><u>U</u></button>
      <button class="umo-bm-btn" :class="{ active: editor.isActive('strike') }" title="Strikethrough" @click="exec('toggleStrike')"><s>S</s></button>
      <span class="umo-bm-sep"></span>
      <!-- Color picker trigger -->
      <div class="umo-bm-color-wrap" title="Text color">
        <span class="umo-bm-color-label" :style="{ color: currentColor }">A</span>
        <input type="color" class="umo-bm-color-input" :value="currentColor" @input="setColor($event.target.value)" />
      </div>
      <div class="umo-bm-color-wrap" title="Highlight">
        <span class="umo-bm-hl-label" :style="{ background: currentHighlight }">H</span>
        <input type="color" class="umo-bm-color-input" :value="currentHighlight" @input="setHighlight($event.target.value)" />
      </div>
      <span class="umo-bm-sep"></span>
      <button class="umo-bm-btn" :class="{ active: isLinkActive }" title="Link" @click="openLink">&#x1F517;</button>
      <span class="umo-bm-sep"></span>
      <button class="umo-bm-btn" title="Clear format" @click="clearFormat">&#x2718;</button>
    </template>
  </div>
</template>

<script setup>
import { ref, watch, onMounted, onUnmounted } from "vue";

var props = defineProps({
  editor: Object,
  linkDialogVisible: Boolean,
});

var emit = defineEmits(["openLink"]);

var bubbleRef = ref(null);
var visible = ref(false);
var isInCodeBlock = ref(false);
var currentColor = ref("#000000");
var currentHighlight = ref("#ffff00");
var isLinkActive = ref(false);

var selectionChangeHandler = null;
var scrollHandler = null;
var blurHandler = null;
var keyHandler = null;

function updatePosition() {
  var ed = props.editor;
  if (!ed || !ed.view) { visible.value = false; return; }
  var sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) { visible.value = false; return; }
  var range = sel.getRangeAt(0);

  // Check if selection is inside code block
  var node = sel.anchorNode;
  var inCode = false;
  while (node) {
    if (node.nodeType === 1) {
      var el = node;
      if (el.classList && (el.classList.contains("umo-task-list") || el.tagName === "PRE" || el.tagName === "CODE")) {
        inCode = true;
        break;
      }
      if (el.getAttribute && el.getAttribute("data-type") === "taskList") {
        inCode = true;
        break;
      }
    }
    node = node.parentNode;
  }
  isInCodeBlock.value = inCode;

  var rect = range.getBoundingClientRect();
  var menu = bubbleRef.value;
  if (!menu) return;

  var menuW = menu.offsetWidth;
  var menuH = menu.offsetHeight;

  var left = rect.left + rect.width / 2 - menuW / 2;
  if (left < 8) left = 8;
  if (left + menuW > window.innerWidth - 8) left = window.innerWidth - menuW - 8;

  var top = rect.top - menuH - 8;
  if (top < 4) {
    top = rect.bottom + 8;
  }

  menu.style.left = left + "px";
  menu.style.top = top + "px";
}

function checkSelection() {
  var ed = props.editor;
  if (!ed || !ed.view) {
    visible.value = false;
    return;
  }

  // Check if editor has focus
  if (!ed.view.hasFocus()) {
    visible.value = false;
    return;
  }

  var sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) {
    visible.value = false;
    return;
  }

  // Check if selection is inside the editor
  var editorEl = ed.view.dom;
  if (!editorEl.contains(sel.anchorNode) || !editorEl.contains(sel.focusNode)) {
    visible.value = false;
    return;
  }

  // Update active states
  currentColor.value = ed.getAttributes("textStyle").color || "#000000";
  var hl = ed.getAttributes("highlight");
  currentHighlight.value = (hl && hl.color) || "#ffff00";
  isLinkActive.value = !!ed.getAttributes("link").href;

  visible.value = true;
  updatePosition();
}

function exec(command) {
  var ed = props.editor;
  if (!ed) return;
  ed.chain().focus()[command]().run();
  ed.view.focus();
}

function setColor(color) {
  var ed = props.editor;
  if (!ed) return;
  ed.chain().focus().setColor(color).run();
  currentColor.value = color;
}

function setHighlight(color) {
  var ed = props.editor;
  if (!ed) return;
  ed.chain().focus().toggleHighlight({ color: color }).run();
  currentHighlight.value = color;
}

function clearFormat() {
  var ed = props.editor;
  if (!ed) return;
  ed.execClearFormat();
  ed.view.focus();
}

function openLink() {
  emit("openLink");
}

function onSelectionChange() {
  checkSelection();
}

function onScroll() {
  if (visible.value) updatePosition();
}

function onBlur() {
  // Delay to allow click inside bubble menu
  setTimeout(function () {
    var ed = props.editor;
    if (!ed || !ed.view || !ed.view.hasFocus()) {
      visible.value = false;
    }
  }, 200);
}

function onKeyDown(e) {
  if (e.key === "Escape" && visible.value) {
    visible.value = false;
    if (props.editor) props.editor.view.focus();
  }
}

watch(function () { return props.linkDialogVisible; }, function (val) {
  if (val) visible.value = false;
});

watch(function () { return props.editor; }, function (ed) {
  if (!ed) return;

  // Use ProseMirror selection update
  ed.on("selectionUpdate", onSelectionChange);
  ed.on("update", onSelectionChange);
  ed.on("blur", onBlur);
  ed.on("focus", function () { setTimeout(checkSelection, 50); });

  scrollHandler = function () {
    if (visible.value) updatePosition();
  };
  document.addEventListener("scroll", scrollHandler, true);
  keyHandler = onKeyDown;
  document.addEventListener("keydown", keyHandler);

  // Clean up old listeners if editor changes
  selectionChangeHandler = function () { checkSelection(); };
});

onUnmounted(function () {
  var ed = props.editor;
  if (ed) {
    ed.off("selectionUpdate", onSelectionChange);
    ed.off("update", onSelectionChange);
    ed.off("blur", onBlur);
  }
  if (scrollHandler) document.removeEventListener("scroll", scrollHandler, true);
  if (keyHandler) document.removeEventListener("keydown", keyHandler);
});
</script>
