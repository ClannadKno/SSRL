<template>
  <span id="saveStatus" class="save-status" :class="statusClass">{{ statusLabel }}</span>
</template>

<script setup>
import { computed } from "vue";

const props = defineProps({
  status: {
    type: String,
    default: "saved",
    validator: (v) => ["saved", "unsaved", "saving", ""].includes(v),
  },
});

const statusClass = computed(() => {
  switch (props.status) {
    case "saved":
      return "saved";
    case "unsaved":
      return "unsaved";
    case "saving":
      return "saving";
    default:
      return "";
  }
});

const statusLabel = computed(() => {
  switch (props.status) {
    case "saved":
      return "已保存";
    case "unsaved":
      return "未保存";
    case "saving":
      return "保存中...";
    default:
      return "";
  }
});
</script>
