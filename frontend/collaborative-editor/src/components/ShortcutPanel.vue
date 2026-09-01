<template>
  <div class="umo-shortcut-panel">
    <div v-for="(group, gi) in shortcutGroups" :key="gi" class="umo-sp-group">
      <div class="umo-sp-group-title">{{ group.title }}</div>
      <div v-for="(item, ii) in group.items" :key="ii" class="umo-sp-row">
        <span class="umo-sp-label">{{ item.label }}</span>
        <span class="umo-sp-keys">
          <kbd v-for="(key, ki) in item.keys" :key="ki">{{ key }}</kbd>
        </span>
      </div>
    </div>
  </div>
</template>

<script setup>
/**
 * ShortcutPanel - Displays available keyboard shortcuts.
 * Shows common text editing and document shortcuts.
 */
var shortcutGroups = [
  {
    title: "Commonly Used",
    items: [
      { label: "Copy", keys: ["Ctrl", "C"] },
      { label: "Paste", keys: ["Ctrl", "V"] },
      { label: "Paste as plain text", keys: ["Ctrl", "Shift", "V"] },
      { label: "Undo", keys: ["Ctrl", "Z"] },
      { label: "Redo", keys: ["Ctrl", "Shift", "Z"] },
      { label: "Select All", keys: ["Ctrl", "A"] },
      { label: "Save", keys: ["Ctrl", "S"] },
      { label: "Print", keys: ["Ctrl", "P"] },
    ],
  },
  {
    title: "Text Formatting",
    items: [
      { label: "Bold", keys: ["Ctrl", "B"] },
      { label: "Italic", keys: ["Ctrl", "I"] },
      { label: "Underline", keys: ["Ctrl", "U"] },
      { label: "Strikethrough", keys: ["Ctrl", "Shift", "X"] },
      { label: "Indent", keys: ["Tab"] },
      { label: "Outdent", keys: ["Shift", "Tab"] },
      { label: "New paragraph", keys: ["Enter"] },
      { label: "Line break", keys: ["Shift", "Enter"] },
    ],
  },
  {
    title: "Search & Navigation",
    items: [
      { label: "Search / Replace", keys: ["Ctrl", "F"] },
      { label: "Find next", keys: ["F3"] },
      { label: "Find previous", keys: ["Shift", "F3"] },
      { label: "Insert link", keys: ["Ctrl", "K"] },
      { label: "Close dialog", keys: ["Esc"] },
    ],
  },
];
</script>

<style scoped>
.umo-shortcut-panel {
  padding: 4px 0;
}

.umo-sp-group {
  margin-bottom: 16px;
}

.umo-sp-group:last-child {
  margin-bottom: 0;
}

.umo-sp-group-title {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--ink-muted, #999);
  padding: 0 12px;
  margin-bottom: 6px;
}

.umo-sp-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 12px;
  font-size: 12px;
  color: var(--ink, #333);
  border-bottom: 1px solid var(--line-soft, #f0f0f0);
}

.umo-sp-row:last-child {
  border-bottom: none;
}

.umo-sp-label {
  color: var(--ink-2, #555);
}

.umo-sp-keys {
  display: flex;
  align-items: center;
  gap: 3px;
}

.umo-sp-keys kbd {
  display: inline-block;
  padding: 2px 6px;
  font-size: 11px;
  font-family: inherit;
  line-height: 1.4;
  color: var(--ink, #333);
  background: var(--accent-dim, #f5f5f5);
  border: 1px solid var(--line, #ddd);
  border-radius: 4px;
  border-bottom-width: 2px;
  white-space: nowrap;
}
</style>
