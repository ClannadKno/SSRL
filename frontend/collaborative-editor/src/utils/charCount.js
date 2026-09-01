/**
 * charCount.js
 *
 * Character counting utility for collaborative editor content.
 * Counts visible text characters in ProseMirror/Yjs document content.
 *
 * Rules:
 * - Counts text nodes (visible characters) including Chinese and English.
 * - Includes headings, paragraphs, lists, task lists, code blocks, blockquotes.
 * - Excludes: internal ProseMirror attributes, link URL text (counts visible link label).
 * - Counts spaces.
 * - Selection counting counts characters in current text selection.
 */

/**
 * Count visible text characters in a ProseMirror JSON document.
 * @param {object} doc - ProseMirror document (JSON format, with type/content/marks attrs)
 * @returns {number} Total character count
 */
export function countChars(doc) {
  if (!doc) return 0;
  var count = 0;
  walkNode(doc, function (node) {
    if (node.type === "text") {
      count += node.text ? node.text.length : 0;
    }
  });
  return count;
}

/**
 * Count visible text characters in a ProseMirror document,
 * within a specific range (for selection counting).
 * @param {object} doc - ProseMirror document (JSON format)
 * @param {number} from - Start position
 * @param {number} to - End position
 * @returns {number} Character count in range
 */
export function countCharsInRange(doc, from, to) {
  if (!doc || from == null || to == null) return 0;
  if (from === to) return 0;
  var count = 0;
  walkNode(doc, function (node, pos) {
    if (node.type === "text" && node.text) {
      var nodeStart = pos;
      var nodeEnd = pos + node.text.length;
      var overlapStart = Math.max(nodeStart, from);
      var overlapEnd = Math.min(nodeEnd, to);
      if (overlapEnd > overlapStart) {
        var textStart = overlapStart - nodeStart;
        var textEnd = overlapEnd - nodeStart;
        count += node.text.slice(textStart, textEnd).length;
      }
    }
  });
  return count;
}

/**
 * Walk a ProseMirror JSON document recursively.
 * Calls callback for each text node with (node, accumulatedPos).
 */
function walkNode(node, callback, pos) {
  if (!node) return;
  pos = pos || 0;
  if (node.type === "text") {
    callback(node, pos);
    return;
  }
  var currentPos = pos;
  if (node.content && Array.isArray(node.content)) {
    for (var i = 0; i < node.content.length; i++) {
      var child = node.content[i];
      walkNode(child, callback, currentPos);
      currentPos = advancePos(currentPos, child);
    }
  }
}

function advancePos(pos, node) {
  if (node.type === "text") {
    return pos + (node.text ? node.text.length : 0);
  }
  if (node.content && Array.isArray(node.content)) {
    for (var i = 0; i < node.content.length; i++) {
      pos = advancePos(pos, node.content[i]);
    }
  }
  return pos;
}

/**
 * Throttled character counter convenience.
 * Returns a function that updates charCount/selCharCount refs at most
 * every `delay` ms.
 *
 * @param {object} charCountRef - Vue ref for total char count
 * @param {object} selCharCountRef - Vue ref for selection char count
 * @param {Function} editorGetter - function that returns the editor instance
 * @param {number} [delay=200]
 * @returns {Function} call this on every editor update
 */
export function createCharCountUpdater(charCountRef, selCharCountRef, editorGetter, delay) {
  if (delay === void 0) delay = 200;
  var lastUpdate = 0;
  var pending = null;

  function update() {
    pending = null;
    lastUpdate = Date.now();
    var editor = editorGetter();
    if (!editor) {
      charCountRef.value = 0;
      selCharCountRef.value = 0;
      return;
    }
    var docJson = editor.getJSON();
    charCountRef.value = countChars(docJson);
    var sel = editor.state.selection;
    if (sel && !sel.empty && sel.from != null && sel.to != null) {
      selCharCountRef.value = countCharsInRange(docJson, sel.from, sel.to);
    } else {
      selCharCountRef.value = 0;
    }
  }

  return function onUpdate() {
    if (pending) return;
    var now = Date.now();
    if (now - lastUpdate >= delay) {
      update();
    } else {
      var wait = delay - (now - lastUpdate);
      pending = setTimeout(update, wait);
    }
  };
}
