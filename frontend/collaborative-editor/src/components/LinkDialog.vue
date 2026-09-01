<template>
  <div v-if="visible" class="umo-link-overlay" @click.self="handleCancel">
    <div class="umo-link-dialog" role="dialog" aria-label="Link" @keydown.esc="handleCancel">
      <div class="umo-link-dialog-header">
        <span>{{ isEditing ? "Edit Link" : "Insert Link" }}</span>
        <button class="umo-link-dialog-close" @click="handleCancel" aria-label="Close">&times;</button>
      </div>
      <div class="umo-link-dialog-body">
        <div class="umo-link-field">
          <label>URL</label>
          <input
            ref="urlInputRef"
            v-model="urlValue"
            type="url"
            placeholder="https://example.com"
            :class="{ 'umo-link-input-error': urlError }"
            @keydown.enter="handleConfirm"
          />
          <span v-if="urlError" class="umo-link-error-msg">{{ urlError }}</span>
        </div>
        <div class="umo-link-field">
          <label>Display Text</label>
          <input
            v-model="textValue"
            type="text"
            placeholder="Link text"
            @keydown.enter="handleConfirm"
          />
        </div>
        <div class="umo-link-field umo-link-field-checkbox">
          <label>
            <input v-model="newWindow" type="checkbox" />
            <span>Open in new window</span>
          </label>
        </div>
      </div>
      <div class="umo-link-dialog-footer">
        <button v-if="isEditing" class="umo-link-btn umo-link-btn-remove" @click="handleRemove">Remove Link</button>
        <div class="umo-link-btn-group">
          <button class="umo-link-btn umo-link-btn-cancel" @click="handleCancel">Cancel</button>
          <button class="umo-link-btn umo-link-btn-confirm" :disabled="!urlValue.trim()" @click="handleConfirm">Confirm</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, watch, nextTick } from "vue";
import { getGlobalToast } from "../composables/useToast.js";

const props = defineProps({
  visible: Boolean,
  editor: Object,
});

const emit = defineEmits(["close"]);

const urlInputRef = ref(null);
const urlValue = ref("");
const textValue = ref("");
const newWindow = ref(true);
const urlError = ref("");
const isEditing = ref(false);
var savedLinkAttrs = null;

function reset() {
  urlValue.value = "";
  textValue.value = "";
  newWindow.value = true;
  urlError.value = "";
  isEditing.value = false;
  savedLinkAttrs = null;
}

watch(function () { return props.visible; }, function (val) {
  if (!val) { reset(); return; }
  nextTick(function () {
    if (urlInputRef.value) urlInputRef.value.focus();
    var ed = props.editor;
    if (!ed) return;
    var linkAttrs = ed.getAttributes("link");
    if (linkAttrs && linkAttrs.href) {
      isEditing.value = true;
      savedLinkAttrs = linkAttrs;
      urlValue.value = linkAttrs.href || "";
      newWindow.value = linkAttrs.target === "_blank";
      textValue.value = ed.state.doc.textBetween(ed.state.selection.from, ed.state.selection.to) || "";
    } else {
      isEditing.value = false;
      savedLinkAttrs = null;
      urlValue.value = "";
      textValue.value = ed.state.doc.textBetween(ed.state.selection.from, ed.state.selection.to) || "";
      newWindow.value = true;
    }
  });
});

function validateUrl(url) {
  url = url.trim();
  if (!url) return "URL cannot be empty";

  // Reject javascript: protocol
  if (/^\s*javascript\s*:/i.test(url)) return "javascript: URLs are not allowed for security reasons";

  // Reject dangerous data URLs
  if (/^\s*data\s*:\s*(text\/html|text\/javascript|application\/x-javascript)/i.test(url)) {
    return "This type of data URL is not allowed";
  }

  return null;
}

function normalizeUrl(url) {
  url = url.trim();
  if (!url) return "";

  // Already has protocol
  if (/^[a-zA-Z][a-zA-Z0-9+\-.]*:\/\//.test(url)) return url;

  // Relative or absolute path
  if (url.startsWith("/") || url.startsWith("./") || url.startsWith("../") || url.startsWith("#")) return url;

  // Mailto
  if (url.includes("@") && !url.startsWith("mailto:")) return "mailto:" + url;

  // Default to https
  if (!url.startsWith("http://") && !url.startsWith("https://")) return "https://" + url;

  return url;
}

function isExternalUrl(url) {
  try {
    var u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch (e) {
    return false;
  }
}

function handleConfirm() {
  var error = validateUrl(urlValue.value);
  if (error) {
    urlError.value = error;
    getGlobalToast().error(error);
    return;
  }
  urlError.value = "";

  var href = normalizeUrl(urlValue.value);
  var ed = props.editor;
  if (!ed) return;

  var linkAttrs = { href: href };
  if (newWindow.value) {
    linkAttrs.target = "_blank";
    if (isExternalUrl(href)) {
      linkAttrs.rel = "noopener noreferrer";
    }
  } else {
    linkAttrs.target = null;
    linkAttrs.rel = null;
  }

  ed.chain().focus().setLink(linkAttrs).run();
  emit("close");
}

function handleRemove() {
  var ed = props.editor;
  if (ed) {
    ed.chain().focus().unsetLink().run();
  }
  emit("close");
}

function handleCancel() {
  urlError.value = "";
  emit("close");
}
</script>
