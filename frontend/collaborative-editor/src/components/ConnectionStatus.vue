<template>
  <div class="conn-status-wrapper">
    <!-- Normal connection state indicator -->
    <div v-if="!isFailed" class="conn-status" :class="'conn-' + computedClass">
      <span class="conn-dot"></span>
      <span class="conn-label">{{ computedLabel }}</span>
    </div>

    <!-- Error state with details -->
    <div v-else class="conn-status conn-failed">
      <span class="conn-dot"></span>
      <span class="conn-label conn-label-error">{{ errorSummary }}</span>
      <span v-if="errorSuggestion" class="conn-suggestion">{{ errorSuggestion }}</span>
    </div>
  </div>
</template>

<script setup>
import { computed } from "vue";

var props = defineProps({
  status: { type: String, default: "idle" },
  syncStatus: { type: String, default: "idle" },
  error: { type: String, default: null },
});

// ------------------------------------------------------------------
// Error categorization
// ------------------------------------------------------------------

var ERROR_MESSAGES = {
  "认证失败，请重新登录": {
    summary: "权限验证未通过",
    suggestion: "请重新登录后再试",
  },
  "权限验证未通过，请重新登录后再试": {
    summary: "权限验证未通过",
    suggestion: "请重新登录后再试",
  },
};

// Extract the most useful part of the error for display
var errorSummary = computed(function () {
  if (!props.error) return "";
  var mapped = ERROR_MESSAGES[props.error];
  if (mapped) return mapped.summary;
  return props.error;
});

var errorSuggestion = computed(function () {
  if (!props.error) return "";
  var mapped = ERROR_MESSAGES[props.error];
  if (mapped) return mapped.suggestion;
  // Provide generic suggestions based on error content
  var msg = props.error;
  if (msg.indexOf("超时") !== -1 || msg.indexOf("timeout") !== -1 || msg.indexOf("Timeout") !== -1) {
    return "请稍后刷新页面重试";
  }
  if (msg.indexOf("网络") !== -1 || msg.indexOf("NetworkError") !== -1) {
    return "请检查网络后刷新页面";
  }
  if (msg.indexOf("权限") !== -1 || msg.indexOf("认证") !== -1) {
    return "如持续失败请联系教师";
  }
  if (msg.indexOf("500") !== -1 || msg.indexOf("502") !== -1 || msg.indexOf("503") !== -1) {
    return "服务器暂时不可用，请稍后刷新页面";
  }
  if (msg.indexOf("404") !== -1 || msg.indexOf("not found") !== -1) {
    return "请刷新页面重试";
  }
  return "请刷新页面或联系教师";
});

var isFailed = computed(function () {
  return props.status === "failed";
});

// ------------------------------------------------------------------
// State mapping
// ------------------------------------------------------------------

// Phase labels (shown as small text above the main status)
var PHASE_LABELS = {
  "loading-current-document": "正在获取协作文档...",
  "requesting-ticket": "正在申请协作连接权限...",
  "connecting-websocket": "正在连接协作服务器...",
  "syncing": "正在同步协作数据...",
  "synced": "协作连接已建立",
  "failed": "协作连接失败",
  "idle": "未连接",
};

var computedLabel = computed(function () {
  if (props.error) {
    return "连接异常 - " + (errorSummary.value || "未知错误");
  }
  var s = props.status;
  if (PHASE_LABELS[s]) return PHASE_LABELS[s];
  // Fallback mappings for legacy status values
  if (s === "connected") return "协作连接已建立";
  if (s === "connecting") return "正在连接协作服务器...";
  if (s === "syncing" || props.syncStatus === "syncing") return "正在同步协作数据...";
  if (s === "error") return "连接异常";
  if (s === "reconnecting") return "正在重新连接...";
  if (s === "offline") return "连接已断开";
  if (s === "destroyed") return "连接已关闭";
  return s;
});

var computedClass = computed(function () {
  if (props.error) return "error";
  var s = props.status;
  if (s === "synced") return "connected";
  if (s === "requesting-ticket" || s === "connecting-websocket" || s === "syncing") return "connecting";
  if (s === "loading-current-document") return "connecting";
  if (s === "failed") return "error";
  if (s === "idle") return "disconnected";
  // Legacy mappings
  if (s === "connected") return "connected";
  if (s === "connecting" || s === "syncing") return "connecting";
  if (s === "error" || s === "destroyed") return "disconnected";
  return "disconnected";
});
</script>

<style>
.conn-status-wrapper {
  display: inline-flex;
  align-items: center;
}
.conn-status {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--editor-text-muted, var(--ui-text-secondary, #62738c));
}
.conn-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
}
.conn-connected .conn-dot { background-color: var(--editor-success, #2f7d58); }
.conn-connecting .conn-dot { background-color: var(--editor-warning, #a9681f); animation: conn-pulse 1.5s ease-in-out infinite; }
.conn-disconnected .conn-dot { background-color: var(--editor-text-muted, #62738c); }
.conn-error .conn-dot { background-color: var(--editor-danger, #b75656); }
.conn-failed .conn-dot { background-color: var(--editor-danger, #b75656); }
.conn-label { white-space: nowrap; }
.conn-label-error { color: var(--editor-danger, #b75656); font-weight: 600; }
.conn-suggestion {
  display: block;
  font-size: 11px;
  color: var(--editor-warning, #a9681f);
  margin-top: 2px;
}
@keyframes conn-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}
</style>
