<template>
  <CollaborativeEditor
    :documentId="documentId"
    :taskId="taskId"
    :sessionNo="sessionNo"
    :apiBase="apiBase"
    :wsUrl="wsUrl"
    :displayName="displayName"
    :permission="permission"
    :userId="userId"
    :userColor="userColor"
  />
</template>

<script setup>
import CollaborativeEditor from "./components/CollaborativeEditor.vue";

defineProps({
  documentId: [String, Number],
  taskId: [String, Number],
  sessionNo: [String, Number],
  apiBase: String,
  wsUrl: String,
  displayName: String,
  permission: String,
  userId: [String, Number],
  userColor: String,
});
</script>