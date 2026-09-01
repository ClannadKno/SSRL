<template>
  <div class="online-members" :class="{ 'om-compact': compact }">
    <div class="online-members-list">
      <div
        v-for="m in displayMembers"
        :key="m.clientId"
        class="om-item"
        :class="{
          'om-active': m.viewing,
          'om-typing': m.isTyping,
          'om-local': m.isLocal,
          'om-teacher': m.role === 'teacher',
        }"
        :title="memberTooltip(m)"
      >
        <span class="om-dot" :style="{ backgroundColor: m.color }"></span>
        <span class="om-name">{{ m.name }}</span>
        <span v-if="m.role === 'teacher'" class="om-role-label">T</span>
        <span v-else-if="m.role === 'observer'" class="om-role-label">O</span>
        <span v-if="m.isTyping" class="om-typing-indicator" title="Typing...">
          <span class="om-typing-dot"></span>
          <span class="om-typing-dot"></span>
          <span class="om-typing-dot"></span>
        </span>
        <span v-if="!m.viewing" class="om-idle-badge">away</span>
      </div>
      <div v-if="displayMembers.length === 0" class="om-empty">No online members</div>
    </div>
  </div>
</template>

<script setup>
import { computed } from "vue";

var props = defineProps({
  members: {
    type: Array,
    default: function () { return []; },
  },
  currentClientId: {
    type: Number,
    default: null,
  },
  compact: {
    type: Boolean,
    default: false,
  },
});

var displayMembers = computed(function () {
  var cutoff = Date.now() - 60000;
  return props.members.filter(function (m) {
    if (m.viewing) return true;
    if (m.updatedAt && m.updatedAt < cutoff) return false;
    return true;
  });
});

function memberTooltip(m) {
  var parts = [];
  parts.push(m.name || "Anonymous");
  if (m.role === "teacher") parts.push("(Teacher)");
  else if (m.role === "observer") parts.push("(Observer)");
  else parts.push("(Student)");
  if (m.isTyping) parts.push("- Typing...");
  if (!m.viewing) parts.push("- Away");
  return parts.join(" ");
}
</script>
