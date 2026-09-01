<template>
  <div class="tiptap-toolbar" id="tiptapToolbar">
    <!-- History -->
    <div class="tb-group">
      <button class="tiptap-btn" title="Undo (Ctrl+Z)" :disabled="!editor || !editor.can().undo()" @click="editor.chain().focus().undo().run()">&#x21A9;</button>
      <button class="tiptap-btn" title="Redo (Ctrl+Y)" :disabled="!editor || !editor.can().redo()" @click="editor.chain().focus().redo().run()">&#x21AA;</button>
    </div>

    <!-- Structure dropdown -->
    <div class="tb-group">
      <div class="tb-dropdown" @click.stop>
        <button class="tiptap-btn tb-dropdown-toggle" title="Style" :class="{ active: showStructure }" @click="toggleStructure">
          {{ currentStyleLabel }} <span class="tb-dd-arrow">&#x25BE;</span>
        </button>
        <div v-if="showStructure" class="tb-dropdown-menu tb-dd-left" @click="showStructure=false">
          <button v-for="s in structureItems" :key="s.id" class="tb-dd-item" :class="{ active: s.active }" @click="s.action">{{ s.label }}</button>
        </div>
      </div>
    </div>

    <!-- Format -->
    <div class="tb-group">
      <button class="tiptap-btn" :class="{ active: editor?.isActive('bold') }" title="Bold (Ctrl+B)" @click="editor.chain().focus().toggleBold().run()"><b>B</b></button>
      <button class="tiptap-btn" :class="{ active: editor?.isActive('italic') }" title="Italic (Ctrl+I)" @click="editor.chain().focus().toggleItalic().run()"><i>I</i></button>
      <button class="tiptap-btn" :class="{ active: editor?.isActive('underline') }" title="Underline (Ctrl+U)" @click="editor.chain().focus().toggleUnderline().run()"><u>U</u></button>
      <button class="tiptap-btn" :class="{ active: editor?.isActive('strike') }" title="Strikethrough" @click="editor.chain().focus().toggleStrike().run()"><s>S</s></button>
      <button class="tiptap-btn" :class="{ active: editor?.isActive('code') }" title="Inline Code" @click="editor.chain().focus().toggleCode().run()">&lt;/&gt;</button>
      <button class="tiptap-btn" :class="{ active: editor?.isActive('codeBlock') }" title="Code Block" @click="editor.chain().focus().toggleCodeBlock().run()">&#x2301;</button>
    </div>

    <!-- Color -->
    <div class="tb-group">
      <label class="tb-color-btn" title="Text color">
        <span class="tb-color-label" :style="{ color: currentColor }">A</span>
        <input type="color" class="tb-native-color" :value="currentColor" @input="setColor($event.target.value)" />
      </label>
      <label class="tb-color-btn tb-hl-btn" title="Highlight">
        <span class="tb-hl-label" :style="{ background: currentHighlight }">H</span>
        <input type="color" class="tb-native-color" :value="currentHighlight" @input="setHighlight($event.target.value)" />
      </label>
    </div>

    <!-- Lists & Alignment -->
    <div class="tb-group">
      <button class="tiptap-btn" :class="{ active: editor?.isActive('orderedList') }" title="Ordered List" @click="editor.chain().focus().toggleOrderedList().run()">1.</button>
      <button class="tiptap-btn" :class="{ active: editor?.isActive('bulletList') }" title="Bullet List" @click="editor.chain().focus().toggleBulletList().run()">&#x2022;</button>
      <button class="tiptap-btn" :class="{ active: editor?.isActive('taskList') }" title="Task List" @click="editor.chain().focus().toggleTaskList().run()">&#x2610;</button>
    </div>

    <!-- Insert -->
    <div class="tb-group">
      <button class="tiptap-btn" :class="{ active: isLinkActive }" title="Link" @click="$emit('openLink')">&#x1F517;</button>
      <button class="tiptap-btn" title="Horizontal Rule" @click="editor.chain().focus().setHorizontalRule().run()">&#x2014;</button>
    </div>

    <!-- More dropdown (low-frequency items) -->
    <div class="tb-group">
      <div class="tb-dropdown" @click.stop>
        <button class="tiptap-btn tb-dropdown-toggle" :class="{ active: showMore }" title="More options" @click="toggleMore">
          &#x22EF; <span class="tb-dd-arrow">&#x25BE;</span>
        </button>
        <div v-if="showMore" class="tb-dropdown-menu" @click="showMore=false">
          <div class="tb-dd-section-label">Font &amp; Size</div>
          <div class="tb-dd-row">
            <select class="tb-dd-select" :value="currentFont" @change="setFont($event.target.value)" title="Font">
              <option v-for="f in fontList" :key="f.value" :value="f.value">{{ f.label }}</option>
            </select>
            <select class="tb-dd-select tb-dd-select-sm" :value="currentFontSize" @change="setFontSize($event.target.value)" title="Size">
              <option v-for="s in fontSizeList" :key="s" :value="s">{{ s }}</option>
            </select>
          </div>
          <div class="tb-dd-section-label">Alignment</div>
          <div class="tb-dd-row">
            <button class="tiptap-btn tb-dd-btn" :class="{ active: currentAlign === 'left' }" title="Align Left" @click="editor.chain().focus().setTextAlign('left').run()">&#x2190;</button>
            <button class="tiptap-btn tb-dd-btn" :class="{ active: currentAlign === 'center' }" title="Center" @click="editor.chain().focus().setTextAlign('center').run()">&#x2194;</button>
            <button class="tiptap-btn tb-dd-btn" :class="{ active: currentAlign === 'right' }" title="Align Right" @click="editor.chain().focus().setTextAlign('right').run()">&#x2192;</button>
            <button class="tiptap-btn tb-dd-btn" :class="{ active: currentAlign === 'justify' }" title="Justify" @click="editor.chain().focus().setTextAlign('justify').run()">&#x21C4;</button>
          </div>
          <div class="tb-dd-section-label">Line Height</div>
          <div class="tb-dd-row">
            <select class="tb-dd-select" :value="currentLineHeight" @change="setLineHeight($event.target.value)" title="Line Height">
              <option v-for="lh in lineHeightList" :key="lh" :value="lh">{{ lh }}</option>
            </select>
          </div>
          <div class="tb-dd-divider"></div>
          <div class="tb-dd-row">
            <button class="tiptap-btn tb-dd-btn" title="Indent" @click="execIndent">&#x21A3; Indent</button>
            <button class="tiptap-btn tb-dd-btn" title="Outdent" @click="execOutdent">&#x21A2; Outdent</button>
          </div>
          <div class="tb-dd-divider"></div>
          <div class="tb-dd-row">
            <button class="tiptap-btn tb-dd-btn" title="Remove formatting" @click="execClearFormat">&#x2718; Clear Format</button>
            <button class="tiptap-btn tb-dd-btn" title="Turn to paragraph" @click="execTurnToParagraph">&#x21A9; Plain Text</button>
          </div>
          <div class="tb-dd-row">
            <button class="tbtn tiptap-btn tb-dd-btn" title="Clear Color" @click="execClearColor">&#x2718; Clear Color</button>
            <button class="tbtn tiptap-btn tb-dd-btn" title="Clear Highlight" @click="execClearHighlight">&#x2718; Clear Highlight</button>
          </div>
        </div>
      </div>
    </div>

    <!-- Format Painter -->
    <div class="tb-group">
      <button class="tiptap-btn format-painter-btn" :class="{ active: isFormatPainterActive }" title="Format Painter" @click="$emit('toggleFormatPainter')">&#x1F58C;</button>
    </div>

    <!-- Word wrap -->
    <div class="tb-group">
      <button class="tiptap-btn" :class="{ active: isWordWrapEnabled }" title="Toggle Word Wrap" @click="$emit('toggleWordWrap')">&#x21A9;</button>
    </div>

    <!-- Search placeholder -->
    <div class="tb-group tb-group-right">



  </div>
  </div>
</template>

<script setup>
import { ref, computed, watch } from "vue";
var props = defineProps({
  editor: Object,
  isFormatPainterActive: { type: Boolean, default: false },
  isWordWrapEnabled: { type: Boolean, default: false },
});

var emit = defineEmits(['openLink', 'openSearch', 'toggleFormatPainter', 'toggleWordWrap']);

// ---- Data ----
var showStructure = ref(false);
var showMore = ref(false);
var currentFont = ref("");
var currentFontSize = ref("");
var currentLineHeight = ref("");
var currentColor = ref("#000000");
var currentHighlight = ref("#ffff00");
var currentAlign = ref("left");

var fontList = [
  { label: "Default", value: "" },
  { label: "SimSun", value: "SimSun" },
  { label: "SimHei", value: "SimHei" },
  { label: "Microsoft YaHei", value: "Microsoft YaHei" },
  { label: "Arial", value: "Arial" },
  { label: "Times New Roman", value: "Times New Roman" },
];
var fontSizeList = ["12", "14", "16", "18", "20", "24", "28", "32"];
var lineHeightList = ["1.0", "1.25", "1.5", "1.75", "2.0"];

// ---- Computed ----
var isLinkActive = computed(function () {
  return props.editor ? !!props.editor.getAttributes("link").href : false;
});

var currentStyleLabel = computed(function () {
  var ed = props.editor;
  if (!ed) return "Style";
  if (ed.isActive("heading")) {
    return "H" + ed.getAttributes("heading").level;
  }
  if (ed.isActive("blockquote")) return "Quote";
  if (ed.isActive("codeBlock")) return "Code";
  return "Text";
});

var structureItems = computed(function () {
  var ed = props.editor;
  return [
    { id: "p", label: "Text", active: ed ? ed.isActive("paragraph") : false, action: function () { ed.chain().focus().setParagraph().run(); } },
    { id: "h1", label: "Heading 1", active: ed ? ed.isActive("heading", { level: 1 }) : false, action: function () { ed.chain().focus().toggleHeading({ level: 1 }).run(); } },
    { id: "h2", label: "Heading 2", active: ed ? ed.isActive("heading", { level: 2 }) : false, action: function () { ed.chain().focus().toggleHeading({ level: 2 }).run(); } },
    { id: "h3", label: "Heading 3", active: ed ? ed.isActive("heading", { level: 3 }) : false, action: function () { ed.chain().focus().toggleHeading({ level: 3 }).run(); } },
    { id: "bq", label: "Blockquote", active: ed ? ed.isActive("blockquote") : false, action: function () { ed.chain().focus().toggleBlockquote().run(); } },
  ];
});

// ---- Methods ----
function toggleStructure() { showStructure.value = !showStructure.value; }
function toggleMore() { showMore.value = !showMore.value; }

function setFont(value) {
  if (!value) props.editor.chain().focus().unsetFontFamily().run();
  else props.editor.chain().focus().setFontFamily(value).run();
}
function setFontSize(value) {
  props.editor.commands.setFontSize(Number(value));
}
function setLineHeight(value) {
  props.editor.commands.setLineHeight(Number(value));
}
function setColor(value) {
  props.editor.chain().focus().setColor(value).run();
  currentColor.value = value;
}
function setHighlight(value) {
  props.editor.chain().focus().toggleHighlight({ color: value }).run();
  currentHighlight.value = value;
}
function execIndent() { props.editor.commands.setIndent(); }
function execOutdent() { props.editor.commands.setOutdent(); }
function execClearFormat() { props.editor.execClearFormat(); }
function execTurnToParagraph() { props.editor.execTurnToParagraph(); }
function execClearColor() { props.editor.chain().focus().unsetColor().run(); }
function execClearHighlight() { props.editor.chain().focus().unsetHighlight().run(); }

// ---- Close dropdowns on outside click ----
function onDocClick(e) {
  var t = e.target;
  if (!t.closest || (!t.closest(".tb-dropdown") && !t.closest(".tiptap-toolbar"))) {
    showStructure.value = false;
    showMore.value = false;
  }
}
if (typeof document !== "undefined") document.addEventListener("click", onDocClick);

// ---- Sync state from editor ----
watch(function () { return props.editor; }, function (ed) {
  if (!ed) return;
  function refresh() {
    var ts = ed.getAttributes("textStyle");
    currentFont.value = (ts && ts.fontFamily) || "";
    currentFontSize.value = (ts && ts.fontSize) || "";
    var p = ed.getAttributes("paragraph");
    currentLineHeight.value = (p && p.lineHeight) || "";

    // Alignment
    var ta = ["left", "center", "right", "justify"];
    for (var i = 0; i < ta.length; i++) {
      if (ed.isActive({ textAlign: ta[i] })) { currentAlign.value = ta[i]; break; }
    }
    if (!currentAlign.value) currentAlign.value = "left";

    currentColor.value = (ts && ts.color) || "#000000";
    var hl = ed.getAttributes("highlight");
    currentHighlight.value = (hl && hl.color) || "#ffff00";
  }
  ed.on("selectionUpdate", refresh);
  ed.on("update", refresh);
  refresh();
});
</script>







