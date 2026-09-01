<template>
  <div class="editor-statusbar">
    <div class="sb-group sb-group-left">
      <!-- Format Painter Status -->
      <span v-if="formatPainterActive" class="sb-item sb-fp-status" title="Format Painter active - click text to apply formatting">
        <span class="sb-fp-icon">&#x1F58C;</span>
        <span class="sb-value">Format Painter</span>
        <button class="sb-fp-cancel" @click="$emit('cancelFormatPainter')" title="Cancel">&times;</button>
      </span>
      <span class="sb-item sb-char-count" title="Total characters">
        <span class="sb-icon">Aa</span>
        <span class="sb-value">{{ charCount.toLocaleString() }}</span>
      </span>
      <span v-if="selCharCount > 0" class="sb-item sb-sel-count" title="Selected characters">
        <span class="sb-icon">Sel</span>
        <span class="sb-value">{{ selCharCount.toLocaleString() }}</span>
      </span>
    </div>

    <div class="sb-group sb-group-right">
      <span v-if="isReadOnly || docStatus === 'submitted' || docStatus === 'locked'" class="sb-item sb-readonly-badge">
        Read Only
      </span>

      <span class="sb-item sb-online-count" title="Online members">
        <span class="sb-dot-list">
          <span
            v-for="(c, i) in onlineColorsSlice"
            :key="i"
            class="sb-avatar-dot"
            :style="{ backgroundColor: c }"
          ></span>
        </span>
        <span class="sb-value">{{ onlineCount }}</span>
        <span class="sb-unit">online</span>
      </span>

      <span
        class="sb-item sb-sync-status"
        :class="'sb-sync-' + syncStatus"
        :title="syncTooltip"
      >
        <span class="sb-sync-icon">{{ syncIcon }}</span>
        <span class="sb-value">{{ syncLabel }}</span>
        <span v-if="lastSyncTimeStr" class="sb-last-sync" :title="'Last sync: ' + lastSyncTimeStr">
          {{ lastSyncTimeStr }}
        </span>
      </span>

      <span
        class="sb-item sb-conn-status"
        :class="'sb-conn-' + connStatusClass"
        :title="'Connection: ' + connLabel"
      >
        <span class="sb-conn-dot"></span>
        <span class="sb-value">{{ connLabel }}</span>
      </span>
    </div>
  </div>
</template>

<script setup>
import { computed } from "vue";

var props = defineProps({
  charCount: { type: Number, default: 0 },
  selCharCount: { type: Number, default: 0 },
  connectionStatus: { type: String, default: "idle" },
  syncStatus: { type: String, default: "idle" },
  lastSyncTime: { type: Number, default: null },
  onlineCount: { type: Number, default: 0 },
  onlineColors: { type: Array, default: function () { return []; } },
  isReadOnly: { type: Boolean, default: false },
  docStatus: { type: String, default: null },
  formatPainterActive: { type: Boolean, default: false },
});

// Map new connectionStatus values to class names
var connStatusClass = computed(function () {
  var s = props.connectionStatus;
  if (s === "synced" || s === "connected") return "connected";
  if (s === "requesting-ticket" || s === "connecting-websocket" || s === "syncing" || s === "connecting") return "connecting";
  if (s === "loading-current-document") return "connecting";
  if (s === "failed" || s === "error" || s === "destroyed") return "error";
  return "disconnected";
});

// Map new connectionStatus values to display labels
var connLabel = computed(function () {
  var s = props.connectionStatus;
  switch (s) {
    case "synced": return "Synced";
    case "connected": return "Connected";
    case "requesting-ticket": return "Connecting...";
    case "connecting-websocket": return "Connecting...";
    case "loading-current-document": return "Loading...";
    case "syncing": return "Syncing...";
    case "connecting": return "Connecting...";
    case "reconnecting": return "Reconnecting...";
    case "offline": return "Offline";
    case "failed": return "Error";
    case "error": return "Error";
    case "destroyed": return "Disconnected";
    default: return "Disconnected";
  }
});

var syncLabel = computed(function () {
  switch (props.syncStatus) {
    case "synced": return "Synced";
    case "syncing": return "Syncing...";
    case "unsynced": return "Unsynced changes";
    case "error": return "Sync error";
    default: return "Not connected";
  }
});

var syncIcon = computed(function () {
  switch (props.syncStatus) {
    case "synced": return "✓";
    case "syncing": return "↻";
    case "unsynced": return "●";
    case "error": return "✗";
    default: return "—";
  }
});

var syncTooltip = computed(function () {
  if (props.lastSyncTime && props.lastSyncTimeStr) {
    return "Last synced: " + props.lastSyncTimeStr;
  }
  return "Sync status: " + props.syncStatus;
});

var lastSyncTimeStr = computed(function () {
  if (!props.lastSyncTime) return null;
  var d = new Date(props.lastSyncTime);
  var h = String(d.getHours()).padStart(2, "0");
  var m = String(d.getMinutes()).padStart(2, "0");
  var s = String(d.getSeconds()).padStart(2, "0");
  return h + ":" + m + ":" + s;
});

var onlineColorsSlice = computed(function () {
  return props.onlineColors.slice(0, 5);
});
var emit = defineEmits(["cancelFormatPainter"]);

if (typeof window !== "undefined") {
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      emit("cancelFormatPainter");
    }
  });
}
</script>
