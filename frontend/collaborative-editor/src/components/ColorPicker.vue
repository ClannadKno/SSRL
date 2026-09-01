<template>
  <div class="umo-color-picker" @click.stop>
    <div class="umo-color-picker-section">
      <div
        v-for="(c, i) in standardColors"
        :key="i"
        class="umo-color-swatch"
        :style="{ backgroundColor: c }"
        :class="{ active: c === (modelValue || "").toLowerCase() }"
        :title="c"
        @click="select(c)"
      ></div>
    </div>
    <div v-if="recentColors.length" class="umo-color-picker-section">
      <div class="umo-color-picker-label">Recent</div>
      <div
        v-for="(c, i) in recentColors"
        :key="'r' + i"
        class="umo-color-swatch"
        :style="{ backgroundColor: c }"
        :title="c"
        @click="select(c)"
      ></div>
    </div>
    <div class="umo-color-picker-actions">
      <button class="umo-color-picker-clear" @click="clear">Clear color</button>
      <label class="umo-color-picker-custom">
        <span>More</span>
        <input type="color" :value="modelValue || "" @input="select($event.target.value)" />
      </label>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from "vue";

var props = defineProps({
  modelValue: String,
});

var emit = defineEmits(["update:modelValue", "change", "clear"]);

var recentColors = ref([]);
const LS_KEY = "umo_editor_recent_colors";

const standardColors = [
  "#000000", "#434343", "#666666", "#999999", "#b7b7b7", "#cccccc", "#d9d9d9", "#efefef", "#f3f3f3", "#ffffff",
  "#980000", "#ff0000", "#ff9900", "#ffff00", "#00ff00", "#00ffff", "#0000ff", "#9900ff", "#ff00ff",
  "#e6b8af", "#f4cccc", "#fce5cd", "#fff2cc", "#d9ead3", "#d0e0e3", "#cfe2f3", "#d9d2e9", "#ead1dc",
  "#dd7e6b", "#ea9999", "#f9cb9c", "#ffe599", "#b6d7a8", "#a2c4c9", "#9fc5e8", "#b4a7d6", "#d5a6bd",
  "#cc4125", "#e06666", "#f6b26b", "#ffd966", "#93c47d", "#76a5af", "#6fa8dc", "#8e7cc3", "#c27ba0",
  "#a61c00", "#cc0000", "#e69138", "#f1c232", "#6aa84f", "#45818e", "#3d85c6", "#674ea7", "#a64d79",
  "#85200c", "#990000", "#b45f06", "#bf9000", "#38761d", "#134f5c", "#0b5394", "#351c75", "#741b47",
  "#5b0f00", "#660000", "#783f04", "#7f6000", "#274e13", "#0c343d", "#073763", "#20124d", "#4c1130",
];

onMounted(function () {
  try {
    var stored = localStorage.getItem(LS_KEY);
    if (stored) {
      var parsed = JSON.parse(stored);
      if (Array.isArray(parsed)) recentColors.value = parsed;
    }
  } catch (e) { /* ignore */ }
});

function saveRecent(color) {
  var arr = recentColors.value.slice();
  var idx = arr.indexOf(color);
  if (idx > -1) arr.splice(idx, 1);
  arr.unshift(color);
  if (arr.length > 10) arr = arr.slice(0, 10);
  recentColors.value = arr;
  try { localStorage.setItem(LS_KEY, JSON.stringify(arr)); } catch (e) { /* ignore */ }
}

function select(color) {
  saveRecent(color);
  emit("update:modelValue", color);
  emit("change", color);
}

function clear() {
  emit("clear");
}
</script>
