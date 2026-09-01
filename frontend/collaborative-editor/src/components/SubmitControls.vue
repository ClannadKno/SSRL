<template>
  <div class="submission-actions">
    <button
      class="submission-btn"
      :class="{ 'btn-submitted': submitted }"
      :disabled="disabled || submitting || submitted"
      @click="$emit('submit')"
    >
      <template v-if="submitted">已提交</template>
      <template v-else-if="submitting">提交中...</template>
      <template v-else>提交成果</template>
    </button>
    <span style="font-size:11px;color:var(--ink-muted)"
      >提交后如有所测问卷将自动跳转</span
    >
  </div>
</template>

<script setup>
defineProps({
  disabled: Boolean,
  submitting: Boolean,
  submitted: Boolean,
});

defineEmits(["submit"]);
</script>
